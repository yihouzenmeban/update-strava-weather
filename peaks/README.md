# 离线山峰库与匹配规则

`peaks.py` 使用本机 OSM `natural=peak` SQLite 数据匹配活动轨迹。数据库不随仓库提交，也不会由同步或 webhook 安装脚本自动下载。

## 新机器准备

从 [RunLogGeoResources 发布页](https://github.com/xxcolfang/RunLogGeoResources/releases/latest) 下载并解压到本目录，保留以下文件名：

- `peaks/RunLogChinaPeaks.sqlite`：中国库。
- `peaks/RunLogGlobalPeaks.sqlite`：全球库。

可使用其中一个库；两者都存在时都会读取，按山峰 ID 去重，中国库优先。所选库应覆盖活动区域。`*.sqlite` 与下载压缩包已 gitignore，勿加入提交。

代码以只读模式查询 `osm_peak` 表，要求列为 `id`、`name`、`elevation_m`、`lat`、`lon`。在仓库根目录执行下例可检查已有库的表结构，不输出山峰明细：

```bash
python3 - <<'PY'
import sqlite3
from pathlib import Path

paths = [Path("peaks") / name for name in
         ("RunLogChinaPeaks.sqlite", "RunLogGlobalPeaks.sqlite")]
paths = [path for path in paths if path.is_file()]
if not paths:
    raise SystemExit("未找到离线山峰库，请先下载解压")
required = {"id", "name", "elevation_m", "lat", "lon"}
for path in paths:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(osm_peak)")}
    if not required <= columns:
        raise SystemExit(f"{path.name}: osm_peak 缺少必需列")
    print(f"{path.name}: 表结构通过")
PY
```

## 匹配与显示

所有有效 GPS 轨迹点参与候选查询、距离确认和经过计数，不按距离抽样。每座候选山峰用一次轨迹遍历同时完成半径判断与经过计数，不重复计算最小距离。至少需要两个有效点；只比较轨迹与山峰的水平距离，不验证登顶高度，也不根据标题猜山名。

| Strava `sport_type` | 进入半径 |
| --- | --- |
| `TrailRun`、`Hike`、`Run`、`Walk` | 100 m |
| `Ride` | 300 m |
| 其它类型（如 `EBikeRide`） | 不参与 |

第一次进入半径计一次，离开至距山峰至少 1,000 m 后再进入才累计。无有效名称的山峰不展示；按经过次数降序、名称升序排列。海拔来自数据库，缺失时省略，经过多次显示 `×N`，例如 `⛰️ 大阳台 (587 m) ×2 • ⛰️ 小阳台`，不再附加 summitbag 署名。

## 描述替换与空结果

启用山峰功能时，先整行移除 summitbag 链接、年度爬升文字，以及以 `⛰` / `🏔` 开头并带括号海拔的旧山峰行，再把新结果放在可识别天气块之前、天气前的手写备注之后。无可识别天气时追加到剩余描述末尾。识别是文本规则，手写内容若符合上述数据行形式也会被删除；同一行包含其它文字也整行删除。

**空结果仍清理旧行。** 缺少数据库、轨迹不足、无匹配、不参与的运动类型，或同步捕获到轨迹获取错误时，均可能走空结果路径。轨迹获取错误会输出警告；缺库和无匹配不一定有警告。数据库查询错误并不都被捕获为空结果，例如表结构错误可能直接使进程失败。

同步缓存、关闭开关、`--force` 对天气和标题的影响，以及低层手动更新与旧版文本排序命令的区别，见 [README 的山峰匹配](../README.md#山峰匹配)。修改此模块后运行 `python3 tests/peaks.py`；同步接入验证运行 `python3 tests/sync.py`。
