#!/usr/bin/env python3

import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("export_simplified_step.py")
SPEC = importlib.util.spec_from_file_location("export_simplified_step", MODULE_PATH)
export_simplified_step = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export_simplified_step)


class StepRewriteTest(unittest.TestCase):
    def test_rewrite_rep_items_accepts_whitespace_before_context_comma(self):
        step = export_simplified_step.Step(
            """ISO-10303-21;
HEADER;
ENDSEC;
DATA;
#1 = SHAPE_REPRESENTATION('',(#11,#53077,#77532), #85167);
#11 = AXIS2_PLACEMENT_3D('',#12,#13,#14);
#53077 = MANIFOLD_SOLID_BREP('',#20);
#77532 = MANIFOLD_SOLID_BREP('',#21);
#85167 = GEOMETRIC_REPRESENTATION_CONTEXT(3);
ENDSEC;
END-ISO-10303-21;
"""
        )

        changed = export_simplified_step.rewrite_rep_items(step, 1, {53077, 77532}, 999)

        self.assertTrue(changed)
        self.assertEqual("SHAPE_REPRESENTATION('',(#11,#999),#85167)", step.bodies[1])


if __name__ == "__main__":
    unittest.main()
