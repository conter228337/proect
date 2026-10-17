"""default_events.csv -> default_targets.csv: одна строка на эмитента (ИНН) с датами первых событий.

Для целевой переменной нужен уровень эмитента, а не выпуска: у одного эмитента может быть несколько
выпусков облигаций, и они уходят в дефолт почти одновременно (4 выпуска = 1 дефолт компании).
Берутся только облигации (kind == 'bond'): невыплата дивидендов и прочее не считаются.

Определения события (разные даты первого события; выбирайте одно как основное, остальные для проверок):
  first_any         первое сообщение о неисполнении по облигациям, включая технический дефолт
  first_default     первое сообщение стадии 'default' (льготный срок истёк / просто «Дефолт»)
  first_accel       первое сообщение о праве требовать досрочного погашения
Дата = дата публикации сообщения (когда информация стала известна рынку), а если её не удалось
разобрать, то дата наступления события из текста. Формат pubDate сайта проверьте на своих данных."""
import sys
import pandas as pd


def _date(series):
    # поэлементно: в столбце бывают и «30.09.2026», и «15.09.2026 17:13», и ISO; общий формат pandas не угадает
    return series.apply(lambda x: pd.to_datetime(x, dayfirst=True, errors="coerce")).dt.normalize()


def build(path_in="default_events.csv", path_out="default_targets.csv"):
    ev = pd.read_csv(path_in, sep=";", decimal=",", encoding="utf-8-sig", dtype=str)
    ev = ev[ev["kind"] == "bond"].copy()
    ev["when"] = _date(ev["pub_date"]).fillna(_date(ev["event_date"]))
    ev["unpaid_rub"] = pd.to_numeric(ev["unpaid_rub"].str.replace(",", "."), errors="coerce")
    ev["issue"] = ev["isin"].where(ev["isin"].notna() & (ev["isin"] != ""), ev["guid"])

    npf = ev[ev["event_type"] == "nonperformance"]
    hard = npf[npf["stage"] == "default"]
    acc = ev[ev["event_type"] == "acceleration"]
    # одно неисполнение = выпуск + обязательство + срок платежа (technical и default по одному платежу считаем раз)
    uniq = npf.drop_duplicates(["inn", "issue", "obligation", "due_date"])

    out = pd.DataFrame({
        "first_any": npf.groupby("inn")["when"].min(),
        "first_default": hard.groupby("inn")["when"].min(),
        "first_accel": acc.groupby("inn")["when"].min(),
        "n_issues": npf.groupby("inn")["issue"].nunique(),
        "n_payments": uniq.groupby("inn").size(),
        "total_unpaid_rub": uniq.groupby("inn")["unpaid_rub"].sum(),
    })
    out["technical_only"] = out["first_default"].isna() & out["first_accel"].isna()
    out = out.reset_index().sort_values("first_any")
    out.to_csv(path_out, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    print(f"эмитентов с событием: {len(out)}, из них только технический дефолт: {int(out['technical_only'].sum())} -> {path_out}")
    return out


if __name__ == "__main__":
    build(*(sys.argv[1:3]))
