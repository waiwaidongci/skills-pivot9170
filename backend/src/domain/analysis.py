"""学员月度归属、报名转化和公开表抑制。"""

from collections import defaultdict
from datetime import date
from itertools import product

from .spec import CHANNELS, PROGRAMS, SPEC

_SUPPRESSED = "已抑制"
_PROGRAM_COLUMNS = {f"program_{index}": program for index, program in enumerate(PROGRAMS, 1)}


def _validated_intakes(rows):
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("受理明细为空，请导入至少一条受理记录。")
    records = []
    groups = defaultdict(list)
    for index, row in enumerate(rows, 1):
        context = f"第 {index} 条受理记录"
        if not isinstance(row, dict):
            raise ValueError(f"{context}的格式有误。")
        learner = row.get("learner_id")
        if not isinstance(learner, str) or not 1 <= len(learner.strip()) <= 80:
            raise ValueError(f"{context}的学员编号须为 1 至 80 个字符。")
        learner = learner.strip()
        day = row.get("intake_date")
        try:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError:
            raise ValueError(f"{context}须填写有效的 YYYY-MM-DD 受理日期。") from None
        channel, program, enrolled = row.get("channel"), row.get("program"), row.get("enrolled")
        if channel not in CHANNELS or program not in PROGRAMS:
            raise ValueError(f"{context}的渠道或项目不在公开名册内。")
        if type(enrolled) is not bool:
            raise ValueError(f"{context}须明确是否已确认报名。")
        records.append(dict(learner_id=learner, intake_date=day, channel=channel, program=program,
                            enrolled=enrolled))
        groups[learner, day[:7]].append((day, channel, program, enrolled))

    intakes = []
    for (_, period), entries in groups.items():
        first_day = min(entry[0] for entry in entries)
        first_assignments = {(channel, program) for day, channel, program, _ in entries if day == first_day}
        if len(first_assignments) != 1:
            raise ValueError("有学员在同月最早受理日存在多个渠道或项目，请核对原始明细后重新导入。")
        channel, program = first_assignments.pop()
        converted = any(project == program and enrolled for _, _, project, enrolled in entries)
        intakes.append((period, channel, program, converted))
    return records, intakes


def validate_rows(rows):
    """验证源明细及月内归属，返回可留存的原始记录副本。"""
    records, _ = _validated_intakes(rows)
    return records


def _parameters(params):
    if not isinstance(params, dict) or set(params) - {field["key"] for field in SPEC["params"]}:
        raise ValueError("公开口径参数有误，请重新设置。")
    month_field, count_field = SPEC["params"]
    period = params.get("period", month_field["default"])
    try:
        if not isinstance(period, str) or date.fromisoformat(f"{period}-01").isoformat()[:7] != period:
            raise ValueError
    except ValueError:
        raise ValueError("统计月份须为有效的 YYYY-MM。") from None
    threshold = params.get("min_count", count_field["default"])
    minimum, maximum = count_field["min"], count_field["max"]
    if type(threshold) is not int or not minimum <= threshold <= maximum:
        raise ValueError(f"公开人数下限须为 {minimum} 至 {maximum} 的整数。")
    return period, threshold


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def _marginal(label, counts):
    enrolled = total = 0
    for cell_enrolled, cell_total in counts:
        enrolled += cell_enrolled
        total += cell_total
    return {"group": label, "enrolled": enrolled, "intakes": total, "rate": f"{enrolled / total:.1%}"}


def analyze(rows, normalized_params):
    """Return only the publication view used by the result page and CSV exporter."""
    period, threshold = _parameters(normalized_params)
    _, intakes = _validated_intakes(rows)
    counts = {key: (0, 0) for key in product(CHANNELS, PROGRAMS)}
    for month, channel, program, converted in intakes:
        if month == period:
            confirmed, total = counts[channel, program]
            counts[channel, program] = (confirmed + int(converted), total + 1)

    published = {key: (confirmed, total) for key, (confirmed, total) in counts.items()
                 if min(confirmed, total - confirmed) >= threshold}
    has_suppression = len(published) != len(counts)
    pivot = []
    series = []
    for channel in CHANNELS:
        row = {"channel": channel}
        for key, program in _PROGRAM_COLUMNS.items():
            cell = published.get((channel, program))
            if cell is None:
                row[key] = _SUPPRESSED
                continue
            confirmed, total = cell
            row[key] = f"{confirmed / total:.1%}（{confirmed} / {total}）"
            series.append({"label": f"{channel} × {program}", "value": round(100 * confirmed / total, 1)})
        pivot.append(row)

    tables = [_table("渠道 × 培训项目", [("channel", "招生渠道"), *_PROGRAM_COLUMNS.items()], pivot)]
    metrics = [{"label": "公开门槛", "value": threshold, "detail": "每格确认与未确认人数各自的下限"}]
    if has_suppression:
        metrics.append({"label": "边际合计", "value": "已省略", "detail": "存在抑制格，省略行、列及总体合计"})
    else:
        overall = _marginal("总体", published.values())
        metrics.append({"label": "总体报名转化率", "value": overall["rate"],
                        "detail": f"{overall['enrolled']} 人确认 / {overall['intakes']} 人受理"})
        columns = [("enrolled", "确认报名人数"), ("intakes", "去重受理人数"), ("rate", "加权转化率")]
        by_channel = [_marginal(channel, (published[channel, program] for program in PROGRAMS))
                      for channel in CHANNELS]
        by_program = [_marginal(program, (published[channel, program] for channel in CHANNELS))
                      for program in PROGRAMS]
        tables.extend([
            _table("渠道合计", [("group", "招生渠道"), *columns], by_channel),
            _table("培训项目合计", [("group", "培训项目"), *columns], by_program),
        ])

    return {
        "state": "warning" if has_suppression else "ok",
        "headline": f"{period} 职业培训报名转化公开表",
        "note": "每格显示转化率（确认报名人数 / 去重受理人数）。抑制格统一标记为「已抑制」。",
        "metrics": metrics,
        "tables": tables,
        "charts": [{"title": "已公开组合的报名转化率（%）", "kind": "bar", "series": series}] if series else [],
        "sections": [{"title": "发布范围", "text": (
            "本表与结果 CSV 仅含公开汇总。学员编号和原始记录留在登录后的数据源。"
            "小单元格抑制不提供跨版本的差分隐私保证，发布前请核对修订版、其他门槛结果和外部资料。"
        )}],
    }
