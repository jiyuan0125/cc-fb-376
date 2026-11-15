#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会员权益档期按月切片。

输入：schedule/ 目录下每周一个版本文件，文件名为版本号（YYYYMMDD），
     字段：会员号,权益码,生效日,到期日,状态,版本号
     状态取值：生效中 / 被替换 / 已终止 / 未生效

输出：monthly/ 下每个自然月一个文件（YYYYMM.csv），
     字段：会员号,权益码,当月有效天数,生效日,到期日,状态
     跨月档期按月拆开，月头、月尾各占一行，分别记录该月内的有效天数；
     生效日、到期日保留该档期实际的有效区间边界。
     另输出 processing_log.csv（版本号,行数,跨月行数）。

语义：
  * 档期两端都含（到期日当天仍可用）。
  * 同一会员+权益码的档期中途被新版本的同权益行替换后，旧行只保留到
    新行生效日的前一天，之后归新行管；旧行状态记为“被替换”。
  * 中途终止的行，其到期日即终止日，终止日之后不再有效；
    未终止部分按终止日截断。
  * “未生效”的行是已排期但尚未生效的未来档期，同样属于有效档期表，
    照常切月输出，状态保留“未生效”。
  * 有效天数 = 实际有效区间在该自然月内的天数（含首尾）。

增量处理：
  * extract_state.json 记录水位（已处理的最大版本号）、已处理版本列表
    和累计的行状态。按版本号升序处理尚未处理过的版本，每处理完一个
    版本立即落盘水位，处理过的版本重跑会被跳过，因此不会产生重复行。
  * 月度文件每次运行都由累计状态整体重建（覆盖写），这样后续版本对
    旧档期的替换/终止能正确反映到之前月份的切片里，也不会留下重复行。
  * processing_log.csv 根据状态中的处理记录重建，每个版本恰好一行；
    正常增量运行时表现为“每处理完一个版本追加一行”。
  * 若把 extract_state.json 删除，则从全部版本重新处理，结果一致。

用法：
    python3 -I extract.py
    python3 -I extract.py --schedule-dir schedule --monthly-dir monthly
    python3 -I extract.py --rebuild          # 忽略状态，从头重算
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Iterator

SCHEDULE_FIELDS = ["会员号", "权益码", "生效日", "到期日", "状态", "版本号"]
MONTHLY_FIELDS = ["会员号", "权益码", "当月有效天数", "生效日", "到期日", "状态"]
LOG_FIELDS = ["版本号", "行数", "跨月行数"]

ST_ACTIVE = "生效中"
ST_REPLACED = "被替换"
ST_TERMINATED = "已终止"
ST_PENDING = "未生效"
VALID_STATUSES = {ST_ACTIVE, ST_REPLACED, ST_TERMINATED, ST_PENDING}

VERSION_FILE_RE = re.compile(r"^\d{8}$")
MONTH_FILE_RE = re.compile(r"^\d{6}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_iso_date(value: str, where: str) -> date:
    if not ISO_DATE_RE.match(value):
        raise DataError(f"{where}: 日期应为 YYYY-MM-DD，实际为 {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataError(f"{where}: 日期不合法 {value!r}") from exc


class DataError(Exception):
    """输入数据或状态文件不合法。"""


@dataclass(frozen=True)
class Row:
    """一条档期行（同一会员+权益码+生效日视为同一行，后续版本可覆盖）。"""

    member: str
    benefit: str
    start: date
    end: date
    status: str
    version: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.member, self.benefit, self.start.isoformat())

    @property
    def crosses_month(self) -> bool:
        return (self.start.year, self.start.month) != (self.end.year, self.end.month)

    def to_state(self) -> dict:
        return {
            "member": self.member,
            "benefit": self.benefit,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": self.status,
            "version": self.version,
        }

    @classmethod
    def from_state(cls, obj: dict) -> "Row":
        try:
            member = str(obj["member"])
            benefit = str(obj["benefit"])
            start = date.fromisoformat(str(obj["start"]))
            end = date.fromisoformat(str(obj["end"]))
            status = str(obj["status"])
            version = str(obj["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError(f"状态文件中的行记录不合法: {obj!r}") from exc
        if status not in VALID_STATUSES:
            raise DataError(f"状态文件中的状态不合法: {status!r}")
        return cls(member, benefit, start, end, status, version)


@dataclass(frozen=True)
class Interval:
    """一条档期最终实际有效的区间。"""

    member: str
    benefit: str
    start: date
    end: date
    status: str


# ---------------------------------------------------------------- 输入读取


def read_schedule_version(path: Path) -> tuple[str, list[Row], int]:
    """读取一个版本文件，返回 (版本号, 行列表, 跨月行数)。"""
    version = path.stem
    if not VERSION_FILE_RE.match(version):
        raise DataError(f"{path.name}: 版本文件名应为 8 位日期（YYYYMMDD）")

    rows: list[Row] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        if headers != SCHEDULE_FIELDS:
            raise DataError(
                f"{path.name}: 表头应为 {','.join(SCHEDULE_FIELDS)}，实际为 {','.join(headers)}"
            )
        for lineno, rec in enumerate(reader, start=2):
            values = {k: (v or "").strip() for k, v in rec.items() if k is not None}
            if not any(values.values()):
                continue  # 容忍空行
            member = values.get("会员号", "")
            benefit = values.get("权益码", "")
            if not member or not benefit:
                raise DataError(f"{path.name}:{lineno}: 会员号/权益码 不能为空")
            start = parse_iso_date(values.get("生效日", ""), f"{path.name}:{lineno} 生效日")
            end = parse_iso_date(values.get("到期日", ""), f"{path.name}:{lineno} 到期日")
            status = values["状态"]
            if status not in VALID_STATUSES:
                raise DataError(
                    f"{path.name}:{lineno}: 状态 {status!r} 不在 {sorted(VALID_STATUSES)} 中"
                )
            if end < start:
                raise DataError(f"{path.name}:{lineno}: 到期日早于生效日")
            row_version = values["版本号"]
            if row_version != version:
                raise DataError(
                    f"{path.name}:{lineno}: 行内版本号 {row_version!r} 与文件名 {version!r} 不一致"
                )
            rows.append(Row(member, benefit, start, end, status, version))

    cross_month = sum(1 for r in rows if r.crosses_month)
    return version, rows, cross_month


def load_state(path: Path) -> tuple[dict[str, Row], list[dict], str | None, list[str]]:
    """返回 (累计行, 处理记录, 水位, 上次产出的月份)。"""
    if not path.exists():
        return {}, [], None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError(f"状态文件 {path} 读取失败: {exc}") from exc
    if not isinstance(data, dict):
        raise DataError(f"状态文件 {path} 结构不合法")

    rows: dict[str, Row] = {}
    for obj in data.get("rows", []):
        row = Row.from_state(obj)
        rows[row.key] = row

    records: list[dict] = []
    seen: set[str] = set()
    for obj in data.get("records", []):
        try:
            version = str(obj["version"])
            count = int(obj["rows"])
            cross = int(obj["cross_month_rows"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError(f"状态文件中的处理记录不合法: {obj!r}") from exc
        if version in seen:
            raise DataError(f"状态文件中版本 {version} 的处理记录重复")
        seen.add(version)
        records.append({"version": version, "rows": count, "cross_month_rows": cross})

    watermark = data.get("watermark")
    if watermark is not None:
        watermark = str(watermark)

    months = [str(m) for m in data.get("months", []) if MONTH_FILE_RE.match(str(m))]
    return rows, records, watermark, months


def read_previous_months(path: Path) -> list[str]:
    """--rebuild 时状态不可信，但仍尝试读出上次产出的月份，用于安全清理。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [str(m) for m in data.get("months", []) if MONTH_FILE_RE.match(str(m))]


def save_state(
    path: Path,
    rows: dict[str, Row],
    records: list[dict],
    watermark: str | None,
    months: Iterable[str] | None = None,
) -> None:
    if months is None:
        months = read_previous_months(path)
    payload = {
        "watermark": watermark,
        "months": sorted(set(months)),
        "records": sorted(records, key=lambda r: r["version"]),
        "rows": [rows[key].to_state() for key in sorted(rows)],
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 版本推进


def apply_version(rows_by_key: dict[str, Row], version: str, rows: Iterable[Row]) -> None:
    """
    把某版本的行并入累计状态；旧版本不得覆盖新版本。

    同一版本内同一 (会员号,权益码,生效日) 只允许留下一行：若同时出现
    “被替换”的重述旧行与更新后的行，以非“被替换”的那行为准（同一版本里
    它才是现行行）；否则数据有歧义，报错。
    """
    incoming: dict[tuple[str, str, str], Row] = {}
    for row in rows:
        prev_incoming = incoming.get(row.key)
        if prev_incoming is None:
            incoming[row.key] = row
        elif prev_incoming.status == ST_REPLACED and row.status != ST_REPLACED:
            incoming[row.key] = row
        elif prev_incoming.status != ST_REPLACED and row.status == ST_REPLACED:
            pass  # 保留现行行
        else:
            raise DataError(
                f"版本 {version} 内出现重复行: {row.member}/{row.benefit}/{row.start}"
            )

    for row in incoming.values():
        prev = rows_by_key.get(row.key)
        if prev is None or prev.version < version:
            rows_by_key[row.key] = row
        # prev.version >= version 且同键：同一版本已被上面合并，更大版本保留


def discover_versions(schedule_dir: Path) -> list[str]:
    if not schedule_dir.is_dir():
        raise DataError(f"档期目录不存在: {schedule_dir}")
    versions: list[str] = []
    for path in sorted(schedule_dir.glob("*.csv")):
        if not VERSION_FILE_RE.match(path.stem):
            print(f"跳过非版本文件: {path.name}", file=sys.stderr)
            continue
        versions.append(path.stem)
    return sorted(versions)


def write_processing_log(path: Path, records: list[dict]) -> None:
    """按状态中的处理记录重建；每个已处理版本恰好一行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(LOG_FIELDS)
        for rec in sorted(records, key=lambda r: r["version"]):
            writer.writerow([rec["version"], rec["rows"], rec["cross_month_rows"]])
    os.replace(tmp, path)


# ---------------------------------------------------------------- 有效区间


def build_intervals(rows_by_key: dict[str, Row]) -> list[Interval]:
    """
    由累计行状态计算每条档期最终实际有效的区间。

    被替换的行：截断到同会员+权益码下一行生效日的前一天；
    若下一行生效日晚于（或等于）本行到期日，则本行不受影响。
    已终止的行：到期日即终止日，原样作为有效区间上界。

    同一会员+权益码的档期按生效日排序后不应重叠。若旧行未标“被替换”
    却与后继行重叠，仍以新版本行为准截断旧行（替换后归新行管），
    同时在 stderr 告警，因为源数据漏了状态标注。
    """
    groups: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows_by_key.values():
        groups[(row.member, row.benefit)].append(row)

    warnings: list[str] = []
    intervals: list[Interval] = []
    for (member, benefit), group in groups.items():
        group.sort(key=lambda r: (r.start, r.version))
        for idx, row in enumerate(group):
            end = row.end
            successor = group[idx + 1] if idx + 1 < len(group) else None
            if successor is not None and successor.start <= end:
                if row.status != ST_REPLACED:
                    warnings.append(
                        f"{member}/{benefit}: {row.start}~{row.end}（状态 {row.status}，"
                        f"版本 {row.version}）与后继行 {successor.start}~{successor.end}"
                        f"（版本 {successor.version}）重叠，按后继行截断"
                    )
                end = successor.start - timedelta(days=1)
            if end < row.start:
                continue  # 被后继行完全覆盖，没有有效天数
            intervals.append(Interval(member, benefit, row.start, end, row.status))

    for msg in warnings:
        print(f"警告: {msg}", file=sys.stderr)
    return intervals


def month_slices(interval: Interval) -> Iterator[tuple[int, int, int]]:
    """按自然月切分区间，产出 (年, 月, 该月内有效天数)。"""
    year, month = interval.start.year, interval.start.month
    while (year, month) <= (interval.end.year, interval.end.month):
        first = date(year, month, 1)
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        last = next_month - timedelta(days=1)
        slice_start = max(interval.start, first)
        slice_end = min(interval.end, last)
        yield year, month, (slice_end - slice_start).days + 1
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


# ---------------------------------------------------------------- 月度输出


def rebuild_monthly(
    monthly_dir: Path, rows_by_key: dict[str, Row], previous_months: Iterable[str] = ()
) -> dict[str, int]:
    intervals = build_intervals(rows_by_key)

    sheets: dict[str, list[tuple]] = defaultdict(list)
    for interval in intervals:
        for year, month, days in month_slices(interval):
            sheets[f"{year:04d}{month:02d}"].append(
                (
                    interval.member,
                    interval.benefit,
                    days,
                    interval.start.isoformat(),
                    interval.end.isoformat(),
                    interval.status,
                )
            )

    monthly_dir.mkdir(parents=True, exist_ok=True)
    produced: set[str] = set()
    for stem, rows in sorted(sheets.items()):
        rows.sort(key=lambda r: (r[0], r[1], r[3], r[4], r[5]))
        path = monthly_dir / f"{stem}.csv"
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(MONTHLY_FIELDS)
            writer.writerows(rows)
        os.replace(tmp, path)
        produced.add(stem)
        print(f"写出 {path.name}: {len(rows)} 行")

    # 只清理上一次运行自己产出、本次不再产出的月份文件，
    # 不动用户自己放进去的其他文件。
    for stem in sorted(set(previous_months) - produced):
        path = monthly_dir / f"{stem}.csv"
        if path.exists():
            path.unlink()
            print(f"删除过期的月度文件: {path.name}")
    return {stem: len(rows) for stem, rows in sheets.items()}


# ---------------------------------------------------------------- 主流程


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="会员权益档期按月切片（按版本增量处理）")
    parser.add_argument("--schedule-dir", default="schedule", help="档期版本目录（默认 schedule）")
    parser.add_argument("--monthly-dir", default="monthly", help="月度切片输出目录（默认 monthly）")
    parser.add_argument(
        "--state-file", default="extract_state.json", help="水位与累计状态文件（默认 extract_state.json）"
    )
    parser.add_argument(
        "--log-file", default="processing_log.csv", help="版本处理记录（默认 processing_log.csv）"
    )
    parser.add_argument("--rebuild", action="store_true", help="忽略已有状态，从全部版本重算")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    schedule_dir = Path(args.schedule_dir)
    monthly_dir = Path(args.monthly_dir)
    state_path = Path(args.state_file)
    log_path = Path(args.log_file)

    versions = discover_versions(schedule_dir)

    if args.rebuild:
        rows_by_key: dict[str, Row] = {}
        records: list[dict] = []
        watermark: str | None = None
        previous_months = read_previous_months(state_path)
        print("--rebuild：忽略已有状态，从全部版本重算")
    else:
        rows_by_key, records, watermark, previous_months = load_state(state_path)

    processed = {rec["version"] for rec in records}
    pending = [v for v in versions if v not in processed]

    if not pending:
        print(f"没有新版本需要处理（水位 {watermark or '无'}，已处理 {len(processed)} 个版本）")

    for version in pending:
        path = schedule_dir / f"{version}.csv"
        _, rows, cross_month = read_schedule_version(path)
        apply_version(rows_by_key, version, rows)
        records.append({"version": version, "rows": len(rows), "cross_month_rows": cross_month})
        watermark = max(watermark or "", version)
        # 每个版本处理完立即落水位，中断重跑不会重复
        save_state(state_path, rows_by_key, records, watermark)
        write_processing_log(log_path, records)
        print(f"处理版本 {version}: {len(rows)} 行，其中跨月 {cross_month} 行")

    write_processing_log(log_path, records)
    month_counts = rebuild_monthly(monthly_dir, rows_by_key, previous_months)
    save_state(state_path, rows_by_key, records, watermark, months=month_counts.keys())

    total = sum(month_counts.values())
    print(f"完成：累计 {len(rows_by_key)} 条档期行，{len(month_counts)} 个月份文件，共 {total} 行切片")
    print(f"状态水位: {watermark or '无'} -> {state_path}")
    print(f"处理记录: {log_path}（{len(records)} 个版本）")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args)
    except DataError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
