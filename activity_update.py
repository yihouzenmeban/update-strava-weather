#!/usr/bin/env python3
"""Calculate changed activity fields and prepare the manual update preview."""

import json
import os
import re
import sys

from peaks import update_peak_line
from project_config import load_config
from weather_template import load_template, render_title, strip_weather


class IndoorActivity(ValueError):
    pass


def build_updates(activity, *, template, title_config, emoji="", description="", peak_line="",
                  reorder_enabled=True, reorder_only=False, map_style="", commute_fix=False, force=False):
    sport_type = activity.get("sport_type") or ""
    trainer = bool(activity.get("trainer"))
    if (trainer or sport_type.startswith("Virtual")) and not force:
        raise IndoorActivity(f"跳过跑步机/室内活动（sport_type={sport_type}, trainer={trainer}），不更新天气")

    name, desc = activity.get("name") or "", activity.get("description") or ""
    new_name = name
    if reorder_only:
        new_desc = desc
    else:
        new_name = render_title(name, emoji.strip(), title_config)
        desc_clean, block = strip_weather(desc, template), description.strip()
        new_desc = (desc_clean + "\n" + block if desc_clean else block) if block else desc_clean
    if reorder_enabled:
        new_desc = update_peak_line(new_desc, peak_line.strip(), template)

    updates = {key: value for key, value, previous in (
        ("name", new_name, name), ("description", new_desc, desc)) if value != previous}
    if commute_fix:
        updates.update(commute=True, hide_from_home=True)
    map_style = map_style.strip()
    if map_style and (updates or reorder_only):
        # Map style cannot be read back; attach it to an existing write or an explicit ancillary update.
        if not re.fullmatch(r"[a-z0-9_]+", map_style):
            raise ValueError("MAP_STYLE 格式无效")
        updates["selected_polyline_style"] = map_style
    return updates


def main(argv):
    activity_path, body_path, activity_id = argv
    try:
        with open(activity_path, encoding="utf-8") as source:
            activity = json.load(source)
    except (OSError, ValueError):
        print("❌ 无法读取有效活动数据", file=sys.stderr)
        return 1
    if not isinstance(activity, dict) or str(activity.get("id")) != activity_id:
        print("❌ 活动 ID 与目标不一致或数据无效", file=sys.stderr)
        return 1

    reorder_only = os.environ.get("STRAVA_REORDER_ONLY") == "1"
    try:
        config = load_config()
        template = load_template(os.environ.get("WEATHER_TEMPLATE_FILE"))
        updates = build_updates(
            activity, template=template, title_config=config["title"],
            emoji=os.environ.get("WEATHER_EMOJI", ""), description=os.environ.get("WEATHER_DESC", ""),
            peak_line=os.environ.get("PEAK_LINE", ""), reorder_enabled=os.environ.get("REORDER_SUMMITBAG") == "1",
            reorder_only=reorder_only, map_style=os.environ.get("MAP_STYLE", ""),
            commute_fix=os.environ.get("STRAVA_COMMUTE_FIX") == "1", force=os.environ.get("FORCE") == "1")
    except IndoorActivity as error:
        print(f"⏭️ {error}\n   如需清理已误标的旧天气标注，请加 FORCE=1")
        return 2
    except ValueError as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1
    if not updates:
        print("⏭️ 没有需要提交的变化，不更新")
        return 3 if reorder_only else 0
    with open(body_path, "w", encoding="utf-8") as output:
        json.dump(updates, output, ensure_ascii=False)
    name, desc = activity.get("name") or "", activity.get("description") or ""
    print("  原标题:", name)
    print("  新标题:", updates.get("name", name))
    if desc:
        print("  原描述:", desc.replace("\n", " | "))
    print("  新描述:")
    print(updates.get("description", desc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
