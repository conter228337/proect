import ast
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

SOURCE = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
TARGETS = {"calculate_volatility", "calculate_financial_ratios"}
NODES = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in TARGETS]
MODULE = ast.Module(body=NODES, type_ignores=[])
namespace = {"np": np, "pd": pd}
exec(compile(MODULE, "main.py", "exec"), namespace)
calculate_volatility = namespace["calculate_volatility"]
calculate_financial_ratios = namespace["calculate_financial_ratios"]

class FinancialMathTests(unittest.TestCase):
    def test_missing_values(self):
        result = calculate_financial_ratios({})
        self.assertTrue(np.isnan(result["ROA"]))
        self.assertTrue(np.isnan(result["Current_Ratio"]))

    def test_ratios(self):
        result = calculate_financial_ratios({
            "net_income": 10, "total_assets": 100, "equity": 50,
            "total_liabilities": 40, "ebitda": 20,
            "current_assets": 30, "current_liabilities": 10, "cash": 5,
        })
        self.assertAlmostEqual(result["ROA"], 10)
        self.assertAlmostEqual(result["ROE"], 20)
        self.assertAlmostEqual(result["Debt_to_EBITDA"], 2)
        self.assertAlmostEqual(result["Current_Ratio"], 3)

    def test_short_volatility_series(self):
        self.assertTrue(np.isnan(calculate_volatility(pd.Series([10]))))

    def test_flat_series_volatility(self):
        self.assertAlmostEqual(calculate_volatility(pd.Series([10, 10, 10])), 0.0)

if __name__ == "__main__":
    unittest.main()
