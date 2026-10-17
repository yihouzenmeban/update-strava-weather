"""Load shared JSON defaults and private partial overrides for every entrypoint."""

import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
ENVIRONMENT = {
    "CLIENT_ID": ("strava", "client_id"),
    "CLIENT_SECRET": ("strava", "client_secret"),
    "WEATHER_TEMPLATE_FILE": ("weather", "template_file"),
    "REORDER_SUMMITBAG": ("weather", "reorder_summitbag"),
    "WEBHOOK_SYNC_DELAY_SECONDS": ("webhook", "sync_delay_seconds"),
    "NGROK_DOMAIN": ("ngrok", "domain"),
}
SHELL_FIELDS = {
    **ENVIRONMENT,
    "OAUTH_TIMEOUT_SECONDS": ("strava", "oauth_timeout_seconds"),
    "STRAVA_LIST_COUNT": ("strava", "list_count"),
    "TOKEN_REFRESH_MARGIN_SECONDS": ("strava", "token_refresh_margin_seconds"),
    "HTTP_CONNECT_TIMEOUT_SECONDS": ("http", "connect_timeout_seconds"),
    "HTTP_REQUEST_TIMEOUT_SECONDS": ("http", "request_timeout_seconds"),
    "WEBHOOK_LISTEN_HOST": ("webhook", "listen_host"),
    "WEBHOOK_LISTEN_PORT": ("webhook", "listen_port"),
    "NGROK_EXECUTABLE": ("ngrok", "executable"),
    "NGROK_CONFIG_FILE": ("ngrok", "config_file"),
    **{"BACKFILL_" + key.upper(): ("backfill", key) for key in (
        "max_rounds", "lock_retry_seconds", "rate_retry_seconds", "fast_failure_seconds",
        "fast_failure_limit", "midnight_grace_seconds")},
}


def read_json(path, private=False):
    try:
        if private:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, encoding="utf-8") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise ValueError("自定义配置必须为当前用户拥有的普通文件，不能使用链接")
                os.fchmod(source.fileno(), 0o600)
                return json.load(source)
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if private:
            return {}
        raise ValueError(f"缺少默认配置：{path.name}") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError(f"无法读取 {path.name}：须为普通文件、不能使用链接，且内容须为有效 UTF-8 JSON") from None


def merge(base, overrides, prefix=""):
    if not isinstance(overrides, dict):
        raise ValueError(f"配置 {prefix or '根节点'} 必须为 JSON 对象")
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        name = f"{prefix}.{key}" if prefix else key
        if key not in base:
            raise ValueError(f"未知配置项：{name}")
        if isinstance(base[key], dict):
            result[key] = merge(base[key], value, name)
        elif type(value) is not type(base[key]):
            raise ValueError(f"配置 {name} 类型错误")
        else:
            result[key] = value
    return result


def validate(config):
    limits = {("sync", "page_size"): (1, 200), ("webhook", "sync_delay_seconds"): (0, None),
              ("webhook", "listen_port"): (1, 65535), ("sync", "lookback_days"): (1, None)}
    for section in ("http", "backfill", "reorder"):
        limits.update({(section, key): (1, None) for key in config[section]})
    limits[("strava", "oauth_timeout_seconds")] = (1, None)
    limits[("strava", "list_count")] = (1, 200)
    limits[("strava", "token_refresh_margin_seconds")] = (0, None)
    limits[("reorder", "page_size")] = (1, 200)
    limits[("reorder", "request_interval_milliseconds")] = (0, None)
    limits[("reorder", "write_interval_milliseconds")] = (0, None)
    for (section, key), (minimum, maximum) in limits.items():
        value = config[section][key]
        if type(value) is not int or value < minimum or maximum is not None and value > maximum:
            raise ValueError(f"配置 {section}.{key} 超出有效整数范围")
    if not config["weather"]["template"].strip():
        raise ValueError("配置 weather.template 不能为空")
    for key in ("executable", "config_file"):
        value = config["ngrok"][key]
        if not value.startswith("/") or any(char in value for char in ('\n', '\r', '\0', '"', '%')):
            raise ValueError(f"配置 ngrok.{key} 必须为有效绝对路径")
    domain = config["ngrok"]["domain"]
    if domain and not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
        raise ValueError("配置 ngrok.domain 只填写域名，不带协议或路径")
    if not re.fullmatch(r"[A-Za-z0-9.:_-]+", config["webhook"]["listen_host"]):
        raise ValueError("配置 webhook.listen_host 无效")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]+", config["webhook"]["path"]):
        raise ValueError("配置 webhook.path 必须为有效 URL 路径")


def load_config(root=None, environ=None):
    root = Path(root) if root is not None else ROOT
    defaults = read_json(root / "config.default.json")
    config = merge(defaults, read_json(root / "config.local.json", private=True))
    environment = os.environ if environ is None else environ
    for name, (section, key) in ENVIRONMENT.items():
        if name not in environment:
            continue
        value = environment[name]
        if type(defaults[section][key]) is bool:
            if value not in ("0", "1"):
                raise ValueError(f"{name} 必须为 0 或 1")
            value = value == "1"
        elif type(defaults[section][key]) is int:
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError(f"{name} 必须为非负整数")
            value = int(value)
        config[section][key] = value
    validate(config)
    return config


def write_local(root, data):
    fd, temporary = tempfile.mkstemp(prefix=".config.local.", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, root / "config.local.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def edit_local(root, update):
    # Lock the directory so concurrent first starts cannot generate different tokens.
    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = read_json(root / "config.local.json", private=True)
        defaults = read_json(root / "config.default.json")
        validate(merge(defaults, data))
        updated = update(data)
        validate(merge(defaults, updated))
        if updated != data:
            write_local(root, updated)
        return updated
    finally:
        os.close(fd)


def ensure_verify_token(root=None):
    root = Path(root) if root is not None else ROOT

    def update(data):
        result = copy.deepcopy(data)
        webhook = result.setdefault("webhook", {})
        if not webhook.get("verify_token"):
            webhook["verify_token"] = secrets.token_urlsafe(32)
        return result

    return edit_local(root, update)["webhook"]["verify_token"]


def legacy_shell(path, names):
    if not path.exists():
        return {}
    # Legacy files were shell scripts; capture only known settings, never source output.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError("旧配置必须为当前用户拥有的普通文件，不能使用链接")
    path.chmod(0o600)
    script = 'set +x; set -a; source "$1" >/dev/null 2>&1 || exit 1; exec python3 -c "$2"'
    extract = "import json,os; print(json.dumps({k:os.environ[k] for k in " + repr(names) + " if k in os.environ}))"
    result = subprocess.run(["/bin/bash", "-c", script, "legacy-config", str(path), extract],
                            cwd=path.parent, env={"PATH": os.environ.get("PATH", os.defpath)},
                            text=True, capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError(f"无法迁移旧配置：{path.name}")
    return json.loads(result.stdout)


def migrate_legacy(root=None):
    root = Path(root) if root is not None else ROOT
    legacy = {}
    values = legacy_shell(root / "strava_config.sh", ("CLIENT_ID", "CLIENT_SECRET", "WEATHER_TEMPLATE_FILE", "REORDER_SUMMITBAG"))
    values.update(legacy_shell(root / "webhook/config.sh", ("NGROK_DOMAIN",)))
    for name, value in values.items():
        section, key = ENVIRONMENT[name]
        if name == "REORDER_SUMMITBAG":
            if value not in ("0", "1"):
                raise ValueError("旧配置 REORDER_SUMMITBAG 必须为 0 或 1")
            value = value == "1"
        legacy.setdefault(section, {})[key] = value
    weather = legacy.get("weather", {})
    if weather.get("template_file"):
        path = Path(weather["template_file"])
        try:
            if str(path) == "templates/weather.default.txt" and not (root / path).exists():
                weather["template"] = read_json(root / "config.default.json")["weather"]["template"]
            else:
                weather["template"] = (path if path.is_absolute() else root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ValueError("无法迁移旧天气模板，请检查文件路径和 UTF-8 编码") from None
        weather["template_file"] = ""
    hook_path = root / "webhook/receiver_config.json"
    if os.path.lexists(hook_path):
        legacy["webhook"] = read_json(hook_path, private=True)

    def update(data):
        # Existing custom values win; repeated migrations are idempotent.
        merged = copy.deepcopy(legacy)
        for section, settings in data.items():
            if not isinstance(settings, dict):
                raise ValueError(f"配置 {section} 必须为 JSON 对象")
            merged.setdefault(section, {}).update(settings)
        return merged

    edit_local(root, update)


def render_units(root, destination, environ=None):
    root, destination = Path(root), Path(destination)
    config = load_config(root, environ=environ)
    if not config["ngrok"]["domain"]:
        raise ValueError("请在 config.local.json 的 ngrok.domain 中填写保留域名")
    host = config["webhook"]["listen_host"]
    if ":" in host:
        host = f"[{host}]"
    replacements = {"__HOOK_DIR__": str(root / "webhook"),
                    "__NGROK_DOMAIN__": config["ngrok"]["domain"],
                    "__NGROK_EXECUTABLE__": config["ngrok"]["executable"],
                    "__NGROK_CONFIG_FILE__": config["ngrok"]["config_file"],
                    "__LISTEN_ADDRESS__": f'{host}:{config["webhook"]["listen_port"]}'}
    destination.mkdir(parents=True, exist_ok=True)
    for source in (root / "webhook/systemd").glob("*.service"):
        content = source.read_text(encoding="utf-8")
        for marker, value in replacements.items():
            content = content.replace(marker, value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%"))
        target = destination / source.name
        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)


def main():
    parser = argparse.ArgumentParser(description="共享配置：默认值 + config.local.json 覆盖")
    parser.add_argument("command", choices=("shell", "migrate", "check", "render-units"))
    parser.add_argument("destination", nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "migrate":
            migrate_legacy()
            print("配置迁移完成；原文件已保留，自定义配置不会提交仓库。")
        elif args.command == "render-units":
            if not args.destination:
                parser.error("render-units 需要指定输出目录")
            render_units(ROOT, args.destination)
        else:
            config = load_config()
            if args.command == "shell":
                for name, (section, key) in SHELL_FIELDS.items():
                    value = config[section][key]
                    value = str(int(value)) if type(value) is bool else str(value)
                    print(f"export {name}={shlex.quote(value)}")
            else:
                print("配置校验通过。")
    except (ValueError, OSError, subprocess.SubprocessError):
        # Never include source text or subprocess output containing credentials.
        error = sys.exc_info()[1]
        print(f"❌ {error}" if isinstance(error, ValueError) else "❌ 配置操作失败，请检查文件权限", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
