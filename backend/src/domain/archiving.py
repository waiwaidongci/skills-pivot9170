"""受理明细的归档聚合与可重算性核对。

归档只保留「月 × 渠道 × 项目 × 是否确认」的人数，不保留学员编号和原始行。
重建出的月度归属与 :func:`src.domain.analysis._validated_intakes` 完全一致，
因此原批在任意统计月份、任意披露门槛下都能重算出同样的月报。
"""

from collections import Counter

from .analysis import _validated_intakes
from .spec import CHANNELS, PROGRAMS


def build_archive(rows):
    """从原始受理行生成归档批次的行（人数聚合）与核对摘要。"""
    records, intakes = _validated_intakes(rows)
    # 每个（月、渠道、项目）去重受理人数，以及其中确认报名的人数。
    totals = Counter()
    confirmed = Counter()
    for period, channel, program, converted in intakes:
        key = (period, channel, program)
        totals[key] += 1
        confirmed[key] += 1 if converted else 0

    archived_rows = []
    for period, channel, program in sorted(totals):
        total = totals[period, channel, program]
        enrolled = confirmed[period, channel, program]
        archived_rows.append({
            "period": period,
            "channel": channel,
            "program": program,
            "intake_count": total,
            "confirmed_count": enrolled,
        })

    original_people = len({(row["learner_id"], row["intake_date"][:7]) for row in records})
    summary = {
        "months": sorted({period for period, *_ in intakes}),
        "intake_people": original_people,
        "archived_people": sum(totals.values()),
        "cells": len(archived_rows),
    }
    return archived_rows, summary


def archive_intakes(archived_rows):
    """把归档聚合行展开回月度归属序列，口径与原始明细的计算路径一致。"""
    if not isinstance(archived_rows, (list, tuple)) or not archived_rows:
        raise ValueError("归档人数为空，无法重算月报。")
    intakes = []
    seen = set()
    for index, row in enumerate(archived_rows, 1):
        context = f"第 {index} 条归档记录"
        if not isinstance(row, dict):
            raise ValueError(f"{context}的格式有误。")
        period, channel, program = row.get("period"), row.get("channel"), row.get("program")
        if not isinstance(period, str) or len(period) != 7 or period[4] != "-":
            raise ValueError(f"{context}的统计月份须为 YYYY-MM。")
        if channel not in CHANNELS or program not in PROGRAMS:
            raise ValueError(f"{context}的渠道或项目不在公开名册内。")
        intake_count, confirmed_count = row.get("intake_count"), row.get("confirmed_count")
        if type(intake_count) is not int or type(confirmed_count) is not int:
            raise ValueError(f"{context}的人数须为整数。")
        if not 1 <= intake_count <= 1_000_000 or not 0 <= confirmed_count <= intake_count:
            raise ValueError(f"{context}的确认人数须介于 0 与受理人数之间。")
        key = (period, channel, program)
        if key in seen:
            raise ValueError(f"{context}与其他归档记录的月份、渠道和项目重复。")
        seen.add(key)
        for _ in range(intake_count - confirmed_count):
            intakes.append((period, channel, program, False))
        for _ in range(confirmed_count):
            intakes.append((period, channel, program, True))
    return intakes


def analyze_archived(archived_rows, normalized_params):
    """归档批次的月报入口：人数聚合 → 月度归属 → 与原批相同的报表计算。"""
    from .analysis import _parameters, analyze_intakes
    period, threshold = _parameters(normalized_params)
    return analyze_intakes(archive_intakes(archived_rows), period, threshold)


def reconcile(original_rows, archived_rows):
    """人数核对：原始与归档重建的月度归属必须逐项一致。"""
    _, original_intakes = _validated_intakes(original_rows)
    rebuilt_intakes = archive_intakes(archived_rows)
    if Counter(original_intakes) != Counter(rebuilt_intakes):
        raise ValueError("归档人数与原批月度归属核对不一致，已中止归档。")
    _, summary = build_archive(original_rows)
    return summary
