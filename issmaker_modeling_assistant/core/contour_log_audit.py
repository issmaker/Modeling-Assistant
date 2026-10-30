"""Pure diagnostics for high-frequency UV Wall contour log events.

Does not change the UV Wall geometry, preflight, or execution frequency.
"""
from collections import Counter


def summarize(lines, limit=100000):
    counts=Counter()
    first_last={}
    total=0
    for line in lines:
        if total>=limit:break
        if "[CONTOUR] PATH_ANALYSIS" in line:
            key="PATH_ANALYSIS"
        elif "[CONTOUR] CHAIN " in line:
            key="CHAIN"
        else:continue
        total+=1
        counts[key]+=1
        message=line.split("] ",2)[-1].strip()
        if message not in first_last:
            first_last[message]=[total,total]
        else:first_last[message][1]=total
    return {"total_contour_entries":total,
            "path_analysis_calls_logged":counts["PATH_ANALYSIS"],
            "chains_logged":counts["CHAIN"],
            "distinct_messages":len(first_last),
            "repeat_entries":max(0,total-len(first_last)),
            "note":"Logging measured; repeated computation not optimized or suppressed"}
