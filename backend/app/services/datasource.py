"""数据源登记、扫描与校验（通道 A/B/C 与三道闸门的第一道）。

对应《数据接入设计说明》§一 通道矩阵：BTS/ISD/WeatherEvents/STPN 走 A（本地批量），
OurAirports/OpenFlights/IEM 走 C（页面直传或本地小文件），清单走 B（只传指令与清单）。
"""
from __future__ import annotations

import csv
import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Config
from app.utils.logger import get_logger
from app.utils.response import ApiError, ErrCode
from app.utils.timeutil import iter_months

log = get_logger(__name__)

BTS_ZIP_RE = re.compile(r"_(\d{4})_(\d{1,2})\.zip$", re.IGNORECASE)
ISD_DIR_RE = re.compile(r"^(\d{4})$")


@dataclass(frozen=True)
class DatasetDef:
    key: str
    name: str
    channel: str          # A 批处理 / B 控制面 / C 页面直传
    rel_glob: str         # 相对 DATA_ROOT 的 glob
    period_kind: str      # year_month / year / none
    target_tables: tuple[str, ...]
    partition_col: str | None
    description: str
    expected_periods: tuple[str, ...] = field(default_factory=tuple)
    file_ext: str = ""
    notes: str = ""

    @property
    def abs_glob(self) -> Path:
        return Path(Config.DATA_ROOT) / self.rel_glob


def _bts_expected() -> tuple[str, ...]:
    return tuple(iter_months("2015-01", "2024-12"))


DATASETS: dict[str, DatasetDef] = {
    d.key: d for d in (
        DatasetDef(
            key="bts", name="BTS 航班准点数据", channel="A",
            rel_glob="01_BTS航班准点数据/On_Time_Reporting_Carrier_On_Time_Performance_1987_present_*.zip",
            period_kind="year_month", target_tables=("ods_bts_flight",), partition_col="year_month",
            description="主表：延误标签 + 运行特征 + 航班链，单月约 22–28 MB / 50–60 万行",
            expected_periods=_bts_expected(), file_ext=".zip",
            notes="Spark 不能直读 zip，由入湖作业按'单月解压→Parquet→删 CSV'执行",
        ),
        DatasetDef(
            key="isd", name="NOAA ISD 逐时气象", channel="A",
            rel_glob="04_NOAA_ISD逐时气象/*/*.csv",
            period_kind="year", target_tables=("ods_isd_weather",), partition_col="year",
            description="全球地面逐小时观测，含中国机场；1142 个站点文件约 6.5 GB",
            file_ext=".csv",
            notes="isd-history.csv 作为机场↔气象站映射，不单独入湖",
        ),
        DatasetDef(
            key="weatherevents", name="US Weather Events", channel="A",
            rel_glob="06_US天气事件/*.zip",
            period_kind="year", target_tables=("ods_weather_event",), partition_col="year",
            description="2016–2022 美国天气事件 862 万条，解压后约 1.04 GB",
            file_ext=".zip",
        ),
        DatasetDef(
            key="ourairports", name="OurAirports 机场数据", channel="C",
            rel_glob="02_OurAirports机场数据/*.csv",
            period_kind="none", target_tables=("dim_airport",), partition_col=None,
            description="airports/runways/navaids，机场经纬度与跑道容量（Public Domain）",
            file_ext=".csv",
            notes="跳过 _根目录副本_待确认/ 下的重复文件",
        ),
        DatasetDef(
            key="openflights", name="OpenFlights 航线与航司", channel="C",
            rel_glob="03_OpenFlights航线数据/*.dat",
            period_kind="none", target_tables=("dim_airline",), partition_col=None,
            description="routes/airports/airlines/planes.dat，航线结构参考（ODbL 需署名）",
            file_ext=".dat",
        ),
        DatasetDef(
            key="iem", name="IEM ASOS 气象样本", channel="C",
            rel_glob="05_IEM_ASOS气象样本/*.csv",
            period_kind="none", target_tables=(), partition_col=None,
            description="5 个枢纽机场 2024 全年逐时观测，用于 ISD 解析器的交叉验证",
            file_ext=".csv",
        ),
        DatasetDef(
            key="stpn", name="STPN 中美航班数据集", channel="A",
            rel_glob="07_STPN中美航班数据集/*/*.npy",
            period_kind="none", target_tables=(), partition_col=None,
            description="中国域 50 机场 × 30 分钟时间片（2015-04~2017-05），跨域泛化测试域",
            file_ext=".npy",
            notes="npy 不入湖，模型侧与跨域接口直读（delay/weather_cn/dist_mx/od_mx）",
        ),
        DatasetDef(
            key="manifest", name="清单与字典", channel="B",
            rel_glob="00_清单与字典/*.csv",
            period_kind="none", target_tables=("ods_file_manifest",), partition_col=None,
            description="manifest.csv / verify_report.csv，文件级 MD5 与行数登记",
            file_ext=".csv",
        ),
        DatasetDef(
            key="dictionary", name="数据字典与说明", channel="B",
            rel_glob="00_清单与字典/数据字典.md",
            period_kind="none", target_tables=(), partition_col=None,
            description="字段口径、清洗规则与下载记录（文档，不入湖）",
        ),
    )
}


def get_dataset(key: str) -> DatasetDef:
    if key not in DATASETS:
        raise ApiError(ErrCode.PARAM_INVALID, f"未知数据集：{key}，可选：{sorted(DATASETS)}")
    return DATASETS[key]


def _period_of(key: str, path: Path) -> str:
    if key == "bts":
        m = BTS_ZIP_RE.search(path.name)
        if not m:
            return ""
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    if key in ("isd", "weatherevents"):
        for part in reversed(path.parts):
            if ISD_DIR_RE.match(part):
                return part
        return ""
    return ""


def scan_files(key: str) -> list[dict]:
    """扫描数据集目录，返回文件清单（不计算 MD5，保证列表接口轻量）。"""
    dataset = get_dataset(key)
    root = Path(Config.DATA_ROOT)
    if not root.exists():
        return []
    files: list[dict] = []
    for path in root.glob(dataset.rel_glob):
        if not path.is_file():
            continue
        if "_根目录副本_待确认" in str(path):
            continue  # 已知重复副本
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        files.append({
            "dataset": key,
            "file_name": path.name,
            "rel_path": rel,
            "period": _period_of(key, path),
            "bytes": stat.st_size,
            "modified": stat.st_mtime,
        })
    files.sort(key=lambda f: (f["period"], f["rel_path"]))
    return files


def file_md5(path: str | Path, chunk: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def load_manifest_csv() -> dict[str, dict]:
    """读取 00_清单与字典/manifest.csv（表头：数据集,相对路径,MB,MD5,修改时间）。"""
    path = Path(Config.DATA_ROOT) / "00_清单与字典" / "manifest.csv"
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rel = (row.get("相对路径") or "").replace("\\", "/")
            if not rel:
                continue
            out[rel] = {
                "dataset": row.get("数据集", ""),
                "md5": (row.get("MD5") or "").strip().lower(),
                "mb": row.get("MB", ""),
                "modified": row.get("修改时间", ""),
            }
    return out


def dataset_status() -> list[dict]:
    """数据集视图：本次扫描到的文件数/体量/覆盖周期/就绪度。"""
    out = []
    for key, ds in DATASETS.items():
        files = scan_files(key)
        periods = sorted({f["period"] for f in files if f["period"]})
        total_bytes = sum(f["bytes"] for f in files)
        expected = list(ds.expected_periods)
        missing = [p for p in expected if p not in periods] if expected else []
        out.append({
            "key": key,
            "name": ds.name,
            "channel": ds.channel,
            "period_kind": ds.period_kind,
            "target_tables": list(ds.target_tables),
            "partition_col": ds.partition_col,
            "description": ds.description,
            "notes": ds.notes,
            "files": len(files),
            "bytes": total_bytes,
            "mb": round(total_bytes / 1024 / 1024, 2),
            "periods_found": len(periods),
            "periods_expected": len(expected),
            "missing_periods": missing[:24],
            "missing_count": len(missing),
            "ready": len(files) > 0,
            "first_period": periods[0] if periods else None,
            "last_period": periods[-1] if periods else None,
        })
    return out


def available_periods(key: str) -> list[str]:
    return sorted({f["period"] for f in scan_files(key) if f["period"]})


def resolve_periods(key: str, requested: list[str] | None, *, mode: str = "incremental") -> list[str]:
    """把请求的 period 列表解析成"实际要处理的文件周期"。

    文件级闸门：请求了不存在的周期 → 直接拦截（对应验收 U-13「触发 2024-13 接入」）。
    """
    ds = get_dataset(key)
    requested = _validate_periods(key, requested)
    if ds.period_kind == "none":
        return [""]
    on_disk = available_periods(key)
    if not on_disk:
        # WeatherEvents 这类"单个包跨多年"的数据集，周期只能从文件内容里解析，
        # 目录名给不出周期 —— 只要有文件就让作业自己按内容分年。
        if scan_files(key):
            return [""]
        raise ApiError(ErrCode.DATA_NOT_READY,
                       f"数据集 {ds.name} 在 {Config.DATA_ROOT} 下未找到任何源文件")
    if not requested:
        if mode == "full":
            return on_disk
        return on_disk[-1:]  # 默认只跑最新一期
    bad = [p for p in requested if p not in on_disk]
    if bad:
        raise ApiError(
            ErrCode.PARAM_INVALID,
            f"以下周期无源文件：{bad}；可选周期：{on_disk[:3]}…{on_disk[-3:] if len(on_disk) > 3 else ''}",
            data={"requested": requested, "missing": bad, "available": on_disk},
        )
    return sorted(set(requested))


def _validate_periods(key: str, periods: list[str] | None) -> list[str] | None:
    """周期写法校验：yyyy-MM / yyyy，非法直接 422（对应验收 U-13）。"""
    ds = get_dataset(key)
    if not periods:
        return periods
    for period in periods:
        text = str(period).strip()
        if ds.period_kind == "year_month" and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
            raise ApiError(ErrCode.PARAM_INVALID,
                           f"周期 {period!r} 非法，应为 yyyy-MM（月份 01–12）")
        if ds.period_kind == "year" and not re.fullmatch(r"\d{4}", text):
            raise ApiError(ErrCode.PARAM_INVALID, f"周期 {period!r} 非法，应为 yyyy")
    return [str(p).strip() for p in periods]


def verify_dataset(key: str, periods: list[str] | None = None, calc_md5: bool = True) -> dict:
    """只读校验：缺失 / 多余 / MD5 不符（§3.1.5 POST /ingest/verify）。"""
    ds = get_dataset(key)
    periods = _validate_periods(key, periods)
    files = scan_files(key)
    if periods:
        files = [f for f in files if f["period"] in set(periods)]
    manifest = load_manifest_csv() if calc_md5 else {}

    checked, mismatch, unknown = [], [], []
    for f in files:
        entry = manifest.get(f["rel_path"])
        if not entry:
            unknown.append(f["rel_path"])
            continue
        item = {"rel_path": f["rel_path"], "bytes": f["bytes"], "mb": round(f["bytes"] / 1024 / 1024, 2),
                "expected_md5": entry["md5"], "period": f["period"]}
        if calc_md5:
            actual = file_md5(Path(Config.DATA_ROOT) / f["rel_path"])
            item["actual_md5"] = actual
            if actual != entry["md5"]:
                mismatch.append(item)
                continue
            item["md5_ok"] = True
        checked.append(item)

    periods_on_disk = sorted({f["period"] for f in scan_files(key) if f["period"]})
    missing_periods = [p for p in ds.expected_periods if p not in periods_on_disk]
    if periods:
        missing_periods = [p for p in periods if p not in periods_on_disk]

    row_wave = _month_over_month_wave(key, files)
    return {
        "dataset": key,
        "name": ds.name,
        "channel": ds.channel,
        "mode": "full" if not periods else "periods",
        "periods": periods or periods_on_disk,
        "files_found": len(files),
        "files_verified": len(checked),
        "files_mismatch": len(mismatch),
        "files_without_manifest": len(unknown),
        "missing_periods": missing_periods,
        "mismatch": mismatch[:50],
        "without_manifest_sample": unknown[:20],
        "bytes": sum(f["bytes"] for f in files),
        "month_over_month_wave": row_wave,
        "passed": len(mismatch) == 0 and not missing_periods,
        "gate": "文件级（MD5 + 字节数 + 周期完整）",
    }


def _month_over_month_wave(key: str, files: list[dict]) -> dict:
    """月环比波动 ≤30% 的文件级闸门（用文件体积做代理指标，避免全量读行数）。"""
    if key != "bts":
        return {"applicable": False}
    by_period = {f["period"]: f["bytes"] for f in files if f["period"]}
    periods = sorted(by_period)
    if len(periods) < 2:
        return {"applicable": False}
    abnormal = []
    for prev, cur in zip(periods, periods[1:]):
        base = by_period[prev] or 1
        wave = (by_period[cur] - base) / base
        if abs(wave) > 0.30:
            abnormal.append({"period": cur, "prev": prev, "wave_pct": round(wave * 100, 2)})
    return {"applicable": True, "abnormal": abnormal[:12], "checked": len(periods) - 1}


# ---------------------------------------------------------------- 解析器
def zip_entries(path: str | Path, max_entry_bytes: int | None = None) -> list[tuple[str, int]]:
    """列出 zip 内容（名, 解压后字节），并做压缩炸弹预检。

    max_entry_bytes 默认取 Config.ZIP_MAX_ENTRY_BYTES（500MB）；
    流式读取（不落盘）的场景可以放宽到单包上限，因为不占磁盘。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = [(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()]
    except zipfile.BadZipFile as exc:
        raise ApiError(ErrCode.PARAM_INVALID, f"{Path(path).name} 不是有效的 zip：{exc}")
    total = sum(size for _, size in infos)
    if total > Config.ZIP_MAX_INFLATE_BYTES:
        raise ApiError(ErrCode.PARAM_INVALID,
                       f"{Path(path).name} 解压后 {total / 1024 / 1024:.0f} MB，超过单包上限 "
                       f"{Config.ZIP_MAX_INFLATE_BYTES / 1024 / 1024:.0f} MB（防压缩炸弹）")
    entry_limit = max_entry_bytes or Config.ZIP_MAX_ENTRY_BYTES
    for name, size in infos:
        if size > entry_limit:
            raise ApiError(ErrCode.PARAM_INVALID,
                           f"{Path(path).name} 内 {name} 解压后 {size / 1024 / 1024:.0f} MB，"
                           f"超过单文件上限 {entry_limit / 1024 / 1024:.0f} MB")
    return infos


def bts_csv_entry(path: str | Path) -> str:
    """取 BTS 压缩包内的主 CSV 名（忽略 readme.html）。"""
    entries = [n for n, _ in zip_entries(path) if n.lower().endswith(".csv")]
    if not entries:
        raise ApiError(ErrCode.PARAM_INVALID, f"{Path(path).name} 内没有 CSV（压缩包损坏？）")
    return entries[0]


def normalize_header(name: str) -> str:
    """R-01 列名归一：去空格、转小写、非字母数字转下划线。"""
    text = (name or "").strip().strip('"').strip()
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    return text.lower()


def bts_header(path: str | Path) -> list[str]:
    """读取 BTS 压缩包内 CSV 的表头（归一化后）。"""
    entry = bts_csv_entry(path)
    with zipfile.ZipFile(path) as zf:
        with zf.open(entry) as fh:
            raw = fh.readline().decode("utf-8", errors="replace")
    return [normalize_header(x) for x in next(csv.reader([raw]))]


# BTS 里需要的字段 → ODS 列（同名直取）
BTS_SOURCE_COLUMNS = [
    "FlightDate", "Reporting_Airline", "Tail_Number", "Flight_Number_Reporting_Airline",
    "Origin", "OriginCityName", "Dest", "DestCityName",
    "CRSDepTime", "DepTime", "DepDelay", "DepDelayMinutes", "DepDel15",
    "CRSArrTime", "ArrTime", "ArrDelay", "ArrDelayMinutes", "ArrDel15",
    "TaxiOut", "TaxiIn", "AirTime", "CRSElapsedTime", "ActualElapsedTime",
    "Cancelled", "CancellationCode", "Diverted", "Distance", "DistanceGroup",
    "CarrierDelay", "WeatherDelay", "NASDelay", "SecurityDelay", "LateAircraftDelay",
]


def read_bts_malformed() -> set[str]:
    """BTS 部分年份的表头有重复列（如 Warning），pandas 会去重成 Warning.1 —— 这里只做提示用。"""
    return {"warning", "warning_1", "warning_2"}



