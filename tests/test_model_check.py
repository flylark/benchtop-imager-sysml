"""Unit tests for tools/model_check.py.  Run:  python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import model_check as mc  # noqa: E402

BASE = """
package T {
    package Requirements {
        requirement <'SR-001'> runTime { doc /* The instrument shall finish a run within 2 hours. */ }
        requirement <'CR-001'> stepSize { doc /* The drive shall move in steps of 0.1 um or smaller. */ }
    }
    package Structure {
        private import ScalarValues::*;
        part def Instrument { part drive : Drive; }
        part def Drive { attribute step_um : Real = 0.05; }
        part inst : Instrument;
        part def Budget {
            attribute half_um : Real = inst.drive.step_um / 2;
            attribute margin_um : Real = 0.1 - half_um;
        }
    }
    package Behavior {
        private import ScalarValues::*;
        action def Run {
            doc /* SR-001 allows 120 minutes. */
            first start;
            then action load { attribute duration_min : Real = 5; }
            then fork f;
            then a;
            then b;
            action a { attribute duration_min : Real = 10; }
            then j;
            action b { attribute duration_min : Real = 8; }
            then j;
            join j;
            then action image { attribute duration_min : Real = 60; }
            then done;
        }
        action run : Run;
    }
    package Verification {
        verification def <'VT-01'> RunTimeTest {
            doc /* Method: Test. Time a full run. Pass: 2 hours or less. */
            subject s : Instrument;
            objective { verify runTime; }
        }
        verification def <'VI-01'> DriveInspection {
            doc /* Method: Inspection. Datasheet review. Pass: step 0.1 um or smaller. */
            subject s : Instrument;
            objective { verify stepSize; }
        }
    }
    package Traceability {
        satisfy runTime by inst;
        satisfy stepSize by inst.drive;
        #derivation connection { end #original ::> runTime; end #derive ::> stepSize; }
        allocate run.load to inst;
        allocate run.a to inst.drive;
        allocate run.b to inst.drive;
        allocate run.image to inst.drive;
    }
}
"""


def check(text):
    return mc.Checker(mc.parse_model(text)).run()


def messages(ch, level=None):
    return [f.message for f in ch.findings if level is None or f.level == level]


class ModelCheckTests(unittest.TestCase):
    def test_clean_model_has_no_findings(self):
        self.assertEqual(messages(check(BASE)), [])

    def test_missing_satisfy_is_an_error(self):
        ch = check(BASE.replace("satisfy stepSize by inst.drive;", ""))
        self.assertTrue(any("CR-001" in m and "satisfy" in m for m in messages(ch, "ERROR")))

    def test_missing_parent_is_an_error(self):
        ch = check(BASE.replace("#derivation connection { end #original ::> runTime; end #derive ::> stepSize; }", ""))
        self.assertTrue(any("CR-001" in m and "parent" in m for m in messages(ch, "ERROR")))

    def test_dead_end_step_is_detected(self):
        text = BASE.replace("action b { attribute duration_min : Real = 8; }\n            then j;",
                            "action b { attribute duration_min : Real = 8; }")
        ch = check(text)
        self.assertTrue(any("'b' has no outgoing flow" in m for m in messages(ch, "ERROR")))

    def test_slack_on_parallel_branch(self):
        model = mc.parse_model(BASE)
        total, _, slack = mc.critical_path(model.action_defs["Run"])
        self.assertEqual(total, 75.0)          # 5 + max(10, 8) + 60
        self.assertEqual(slack["a"], 0.0)
        self.assertEqual(slack["b"], 2.0)

    def test_unresolvable_chain_is_an_error(self):
        ch = check(BASE.replace("satisfy stepSize by inst.drive;", "satisfy stepSize by inst.motor;"))
        self.assertTrue(any("no feature named 'motor'" in m for m in messages(ch, "ERROR")))

    def test_verification_value_mismatch_is_a_warning(self):
        ch = check(BASE.replace("Pass: step 0.1 um or smaller.", "Pass: step 0.2 um or smaller."))
        self.assertTrue(any("does not state 0.1 µm" in m for m in messages(ch, "WARNING")))

    def test_unallocated_function_is_a_warning(self):
        ch = check(BASE.replace("allocate run.image to inst.drive;", ""))
        self.assertTrue(any("run.image is not allocated" in m for m in messages(ch, "WARNING")))

    def test_budget_is_evaluated_from_design_values(self):
        ev = mc.Evaluator(mc.parse_model(BASE))
        self.assertAlmostEqual(ev.attr("Budget", "half_um"), 0.025)
        self.assertAlmostEqual(ev.attr("Budget", "margin_um"), 0.075)

    def test_vague_word_is_flagged(self):
        ch = check(BASE.replace("finish a run within 2 hours", "finish a run fast, within 2 hours"))
        self.assertTrue(any('vague word "fast"' in m for m in messages(ch, "WARNING")))


if __name__ == "__main__":
    unittest.main()
