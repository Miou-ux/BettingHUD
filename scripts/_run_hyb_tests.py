#!/usr/bin/env python3
import tests.test_hybrid_pick_selection as t

t.test_p80_addon_included_without_ev_filter()
t.test_1d1p_best_proba_not_first_rank()
t.test_dedupe_match_between_p75_and_p80()
t.test_legacy_hybrid_still_available()
t.test_hybrid_criteria_plain_mentions_p75_p80()
print("all_tests_ok")
