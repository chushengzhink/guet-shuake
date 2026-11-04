Initial inventory: GUI, cancellation and packaging added since prior review.
GUI findings to verify: stop uses GET but backend POST-only; account creation persists before validation; start bypasses validate; selected courses from first account applied to every logged-in account; status concurrency and worker outcomes discarded.
Core findings: callback mismatch confirmed by signatures; cancel loops break without recording unfinished jobs; tick waits full interval; VideoJob full-range/99% fallback may retry oversized full video indefinitely; scheduler loses completed course results if later course raises.
Complete findings and proposed acceptance criteria are consolidated in docs/review-2026-11-04/review.md; machine-readable evidence is in evidence.json.
