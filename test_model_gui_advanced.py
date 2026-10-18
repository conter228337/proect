import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_model_gui_advanced_cli_generates_outputs(tmp_path):
    input_csv = ROOT / "company_analysis_auto_growth.csv"
    output_csv = tmp_path / "forecast.csv"
    backtest_csv = tmp_path / "backtest.csv"

    result = subprocess.run(
        [
            sys.executable,
            "model_gui_advanced.py",
            "--input",
            str(input_csv),
            "--output",
            str(output_csv),
            "--backtest-output",
            str(backtest_csv),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert output_csv.exists(), "forecast output was not created"
    assert backtest_csv.exists(), "backtest output was not created"
