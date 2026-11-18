"""机场 ↔ 气象站映射（§5.4 气象对齐策略第 1 步）。

链路：机场 IATA → ICAO（OurAirports）→ ISD 站号（isd-history.csv 的 USAF+WBAN）。
5 个中国机场无站点（ZUH/YNT 等），必须显式标注 weather_available=0，不能静默填均值。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from app.config import Config
from app.utils.logger import get_logger

log = get_logger(__name__)

TARGET_COUNTRIES = {"US", "CN"}


@lru_cache(maxsize=1)
def airport_icao_map() -> dict[str, str]:
    """IATA → ICAO（只保留 US/CN 且有 IATA 的机场）。"""
    path = Path(Config.DATA_ROOT) / "02_OurAirports机场数据" / "airports.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path, usecols=["ident", "icao_code", "iata_code", "iso_country"],
                     dtype=str, keep_default_na=False, low_memory=False)
    df = df[df["iso_country"].isin(TARGET_COUNTRIES)]
    df["iata"] = df["iata_code"].str.upper().str.strip()
    df = df[df["iata"].str.fullmatch(r"[A-Z]{3}", na=False)]
    out: dict[str, str] = {}
    for _, row in df.iterrows():
        icao = (row["icao_code"] or row["ident"] or "").upper().strip()
        if len(icao) >= 3:
            out.setdefault(row["iata"], icao)
    return out


@lru_cache(maxsize=1)
def isd_history() -> pd.DataFrame:
    path = Path(Config.DATA_ROOT) / "04_NOAA_ISD逐时气象" / "isd-history.csv"
    if not path.exists():
        return pd.DataFrame(columns=["station_id", "icao", "begin", "end", "name", "country"])
    df = pd.read_csv(path, dtype=str, keep_default_na=False, low_memory=False)
    df.columns = [c.strip().upper() for c in df.columns]
    df["station_id"] = df["USAF"].str.strip() + df["WBAN"].str.strip()
    df["icao"] = df["ICAO"].str.upper().str.strip()
    df["name"] = df["STATION NAME"].str.strip()
    df["country"] = df["CTRY"].str.strip()
    return df


@lru_cache(maxsize=1)
def icao_station_map() -> dict[str, str]:
    """ICAO → 站号，选覆盖年份最长的记录（同机场多站点时避免选到已停用站点）。"""
    df = isd_history()
    if df.empty:
        return {}
    df = df[df["icao"].str.len() >= 3]
    df = df.assign(begin_num=pd.to_numeric(df["BEGIN"], errors="coerce").fillna(0),
                   end_num=pd.to_numeric(df["END"], errors="coerce").fillna(0))
    df = df.sort_values(["begin_num", "end_num"])
    out: dict[str, str] = {}
    for _, row in df.iterrows():
        out[row["icao"]] = row["station_id"]  # 后写覆盖，保留覆盖窗口最长（begin 最大）的
    return out


@lru_cache(maxsize=1)
def station_airport_map() -> dict[str, str]:
    """ISD 站号 → 机场 IATA。"""
    stations = icao_station_map()
    out: dict[str, str] = {}
    for iata, icao in airport_icao_map().items():
        station = stations.get(icao)
        if station:
            out.setdefault(station, iata)
    return out


def mapping_report() -> dict:
    stations = station_airport_map()
    by_country: dict[str, int] = {}
    icao = airport_icao_map()
    for iata in stations.values():
        country = "CN" if iata[0] == "Z" or iata in _CHINA_HINTS else "US"
        by_country[country] = by_country.get(country, 0) + 1
    return {
        "airports_with_icao": len(icao),
        "stations_mapped": len(stations),
        "by_country_estimate": by_country,
        "note": "机场→ICAO→站号；无站点的机场在 DWD 标 weather_available=0",
    }


# 中国机场 IATA 常见首字母（Z 为主，另有部分沿用旧码），仅用于统计口径估算
_CHINA_HINTS = {"PEK", "PVG", "SHA", "CAN", "CTU", "KMG", "XIY", "CKG", "HGH", "NKG",
                "SZX", "WUH", "CSX", "XMN", "TAO", "TSN", "DLC", "SYX", "HAK", "URC",
                "HRB", "SHE", "CGO", "TSN", "FOC", "NNG", "KWE", "TYN", "LXA", "ZUH", "YNT"}


def available_station_files(year: str) -> list[Path]:
    """该年份下、能映射到机场的 ISD 站点文件（避免读全部 1142 个文件）。"""
    root = Path(Config.DATA_ROOT) / "04_NOAA_ISD逐时气象" / str(year)
    if not root.exists():
        return []
    wanted = set(station_airport_map())
    files = [p for p in root.glob("*.csv") if p.stem in wanted]
    log.info("年份 %s：命中 %d / %d 个站点文件", year, len(files), len(list(root.glob('*.csv'))))
    return files
