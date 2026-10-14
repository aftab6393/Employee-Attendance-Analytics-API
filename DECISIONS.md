# Design decisions

1. **Indexes.** Employees have unique `emp_code`, department/list, department/join-date, and `joined_on` indexes for identity, filtering, and headcount. Attendance has unique `(emp_code, date)` for the one-record rule, `(date, emp_code)` for listing, and date/status indexes for analytics filters. I considered indexing each derived value, but analytics first constrain dates, so that would add write cost without helping the main scans.
2. **Punch-in race.** Both requests find the employee and calculate the same attendance date. MongoDB's unique `(emp_code, date)` index lets one insert succeed with 201; duplicate-key for the other becomes 409.
3. **Ties.** `$rank` gives tied totals the same rank and skips the following rank. Filtering rank `<= limit` returns everyone tied at the cutoff; `emp_code` orders those rows.
4. **Headcount.** The monthly department pipeline begins with eligible employees and joins their month logs, preserving people with no logs and excluding future joiners.
5. **100x data.** I would measure query plans and latency, then consider precomputed daily summaries to reduce repeated scans while keeping raw records as the source of truth.
