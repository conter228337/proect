import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pandas as pd

from forecast_credit_risk import (
    backtest_forecasts,
    generate_forecasts,
    validate_training_data,
)


class ForecastModelApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Credit Risk / Rating Forecast Studio")
        self.geometry("980x720")
        self.minsize(900, 620)

        self.data_path = tk.StringVar(value="")
        self.panel = pd.DataFrame()
        self.forecast = pd.DataFrame()

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)

        row = ttk.Frame(main)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="CSV с панелью данных:", width=18, anchor="w").pack(side="left")
        entry = ttk.Entry(row, textvariable=self.data_path, width=70)
        entry.pack(side="left", fill="x", expand=True, padx=(8, 6))
        ttk.Button(row, text="Обзор", command=self._browse_csv).pack(side="left")

        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(0, 12))
        ttk.Button(actions, text="Проверить данные", command=self._validate_data).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Обучить модель", command=self._train_model).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Экспортировать прогноз", command=self._export_forecast).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Сохранить backtest", command=self._export_backtest).pack(side="left")

        summary = ttk.LabelFrame(main, text="Статус")
        summary.pack(fill="x", pady=(0, 12))
        self.status_box = tk.Text(summary, height=10, wrap="word", bg="#f7f7f7")
        self.status_box.pack(fill="both", expand=True, padx=8, pady=8)

        table_frame = ttk.LabelFrame(main, text="Результаты прогноза")
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, show="headings", columns=("inn", "year", "prob", "rating", "status_default", "status_rating"))
        self.tree.heading("inn", text="ИНН")
        self.tree.heading("year", text="Год")
        self.tree.heading("prob", text="PD")
        self.tree.heading("rating", text="Рейтинг")
        self.tree.heading("status_default", text="Default status")
        self.tree.heading("status_rating", text="Rating status")
        self.tree.column("inn", width=120, anchor="center")
        self.tree.column("year", width=80, anchor="center")
        self.tree.column("prob", width=120, anchor="center")
        self.tree.column("rating", width=120, anchor="center")
        self.tree.column("status_default", width=180, anchor="center")
        self.tree.column("status_rating", width=180, anchor="center")

        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        yscroll.pack(side="right", fill="y", pady=8)
        xscroll.pack(side="bottom", fill="x", padx=8)

        self._log("Графический интерфейс запущен. Выберите CSV с панелью данных и начните обучение.")

    def _log(self, text: str):
        self.status_box.insert("end", text + "\n")
        self.status_box.see("end")
        self.update_idletasks()

    def _browse_csv(self):
        path = filedialog.askopenfilename(
            title="Выберите CSV с панелью данных",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        self.data_path.set(path)
        self._log(f"Выбран файл: {path}")

    def _read_panel(self) -> pd.DataFrame:
        path = self.data_path.get().strip()
        if not path:
            raise ValueError("Сначала выберите CSV с данными.")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Файл не найден: {path}")
        return pd.read_csv(path, sep=";", decimal=",", encoding="utf-8-sig")

    def _validate_data(self):
        try:
            panel = self._read_panel()
            report = validate_training_data(panel)
            summary = (
                f"Компаний: {report['companies']}\n"
                f"Строк в панели: {report['panel_rows']}\n"
                f"Пары для обучения: {report['training_pairs']}\n\n"
                f"Default ready: {report['default']['ready']} | "
                f"rows={report['default']['rows']}, defaults={report['default']['defaults']}, "
                f"non_defaults={report['default']['non_defaults']}\n"
                f"Rating ready: {report['rating']['ready']} | "
                f"rows={report['rating']['rows']}, unique={report['rating']['unique_ratings']}\n\n"
                f"Overall ready: {report['overall_ready']}"
            )
            if report["default"]["reason"]:
                summary += f"\nDefault: {report['default']['reason']}"
            if report["rating"]["reason"]:
                summary += f"\nRating: {report['rating']['reason']}"
            self.status_box.delete("1.0", "end")
            self._log(summary)
            self.panel = panel
        except Exception as exc:
            self._log(f"Ошибка валидации: {exc}")
            messagebox.showerror("Ошибка", str(exc))

    def _train_model(self):
        try:
            if self.panel.empty:
                self.panel = self._read_panel()
            prediction = generate_forecasts(self.panel)
            self.forecast = prediction
            self._render_forecast(prediction)
            self._log(f"Обучение завершено. Сформировано {len(prediction)} строк прогноза.")
        except Exception as exc:
            self._log(f"Ошибка обучения: {exc}")
            messagebox.showerror("Ошибка обучения", str(exc))

    def _render_forecast(self, forecast: pd.DataFrame):
        for item in self.tree.get_children():
            self.tree.delete(item)

        if forecast.empty:
            return

        limited = forecast.head(200).copy()
        for _, row in limited.iterrows():
            self.tree.insert(
                "",
                "end",
                values=(
                    str(row.get("inn", "")),
                    row.get("feature_year", ""),
                    f"{row.get('estimated_default_probability', float('nan')):.4f}" if pd.notna(row.get("estimated_default_probability", pd.NA)) else "",
                    row.get("predicted_rating", ""),
                    row.get("default_model_status", ""),
                    row.get("rating_model_status", ""),
                ),
            )

    def _export_forecast(self):
        if self.forecast.empty:
            messagebox.showwarning("Нет данных", "Сначала обучите модель и сформируйте прогноз.")
            return
        default_name = "credit_risk_forecasts.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV файлы", "*.csv")],
        )
        if not path:
            return
        self.forecast.to_csv(path, index=False, sep=";", decimal=",", encoding="utf-8-sig")
        self._log(f"Прогноз сохранён: {path}")

    def _export_backtest(self):
        try:
            if self.panel.empty:
                self.panel = self._read_panel()
            backtest = backtest_forecasts(self.panel)
            default_name = "credit_risk_backtest.csv"
            path = filedialog.asksaveasfilename(
                defaultextension=".csv",
                initialfile=default_name,
                filetypes=[("CSV файлы", "*.csv")],
            )
            if not path:
                return
            backtest.to_csv(path, index=False, sep=";", decimal=",", encoding="utf-8-sig")
            self._log(f"Backtest сохранён: {path}")
        except Exception as exc:
            self._log(f"Ошибка экспорта backtest: {exc}")
            messagebox.showerror("Ошибка", str(exc))


if __name__ == "__main__":
    app = ForecastModelApp()
    app.mainloop()
