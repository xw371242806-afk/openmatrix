"""Deterministic answer/quote consistency checks, separate from quote lookup."""
import re

_NUMBER = re.compile(r"(?<![\w])([-+]?\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand|trillion|bn|b|m|k|%|percent|千|百万|十亿)?(?!\w)", re.I)
_SCALE = {"thousand": 1e3, "k": 1e3, "千": 1e3, "million": 1e6, "m": 1e6,
          "百万": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9, "十亿": 1e9, "trillion": 1e12}

def _numbers(text, page_scale=1):
    result = []
    for match in _NUMBER.finditer(str(text or "")):
        raw = match.group(1)
        value = float(raw.replace(",", ""))
        unit = (match.group(2) or "").lower()
        # Dates and fiscal-year qualifiers are context, rather than answer amounts.
        if not unit and "," not in raw and "." not in raw and 1900 <= value <= 2100:
            continue
        percent = unit in {"%", "percent"}
        scale = _SCALE.get(unit, 1 if percent else page_scale)
        result.append((value * scale, percent, value, bool(unit or page_scale != 1)))
    return result

def assess_answer_support(question, answer, quote, source_page):
    def result(status, reason):
        return {"support_status": status, "support_reason": reason}
    if not answer or not quote or not source_page:
        return result("unchecked", "An answer and a located source quote are required.")
    page = str(source_page).lower()
    scale = 1e3 if re.search(r"in\s+thousands", page) else 1e6 if re.search(r"in\s+millions", page) else 1
    quoted = _numbers(quote, scale)
    answered = _numbers(answer)
    for number, percent, raw, explicit in answered:
        if not any(percent == qpercent and (
            abs(number - qnumber) <= max(abs(qnumber) * .005, 1e-9)
            or (not qexplicit and abs(raw - qraw) <= max(abs(qraw) * .005, 1e-9))
        ) for qnumber, qpercent, qraw, qexplicit in quoted):
            return result("unsupported", "An answer number or unit is not supported by the quote.")
    q = str(question).lower()
    normalized_quote = re.sub(r"\s+", " ", str(quote).lower())
    labels = {
        "total revenue": ("total revenue", "total revenues"),
        "net income": ("net income",),
        "same-shack": ("same-shack", "comparable sales", "same-store"),
        "same-store": ("same-store", "same-shack", "comparable sales"),
        "headcount": ("headcount", "employees", "workforce"),
    }
    for metric, aliases in labels.items():
        if metric in q and not any(alias in normalized_quote for alias in aliases):
            return result("partial", "The quote lacks the requested metric label.")
    if not answered:
        return result("unchecked", "No numeric claim was checked; semantic review is required.")
    return result("supported", "The quote contains the answer numbers and requested metric label.")
