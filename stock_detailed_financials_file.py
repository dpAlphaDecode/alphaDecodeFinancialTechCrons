# Complete Financials ETL Python Script
import math
import os
from typing import Any
import json
import pandas as pd
from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values
from sqlalchemy.orm import sessionmaker
from datetime import date



def extract_financial_metadata(data, frequency):

    labels = data["Label"].astype(str).str.strip()

    # Financial year start
    fy_row = data.loc[
        labels == "DateOfStartOfFinancialYear"
    ]


    if fy_row.empty:
        print("fin date not found")
        return None

    fy_start = pd.to_datetime(
        fy_row.iloc[0]["Value"]
    )

    fy_year = fy_start.year

    # Reporting quarter
    quarter_row = data.loc[
        labels == "ReportingQuarter"
    ]

    if quarter_row.empty:
        return None

    reporting_quarter = str(
        quarter_row.iloc[0]["Value"]
    ).strip()
    print(reporting_quarter)
    #2023-01-01
    quarter_map = {
        "first quarter": ("Q1", date(fy_year, 6, 30)),
        "second quarter": ("Q2", date(fy_year, 9, 30)),
        "third quarter": ("Q3", date(fy_year, 12, 31)),
        "fourth quarter": ("Q4", date(fy_year + 1, 3, 31)),
        # "Half yearly":("",date(fy_year, 9, 31)),
        "yearly":("", date(fy_year + 1, 3, 31)),
    }
    if frequency == "quarterly":
        if reporting_quarter not in quarter_map:
            print("quarter map not found")
            if str(data.loc[labels == "DateOfStartOfReportingPeriod"].iloc[0]["Value"]).endswith("04-01"):
                quarter, report_date = "Q1",  date(fy_year, 6, 30)
            elif str(data.loc[labels == "DateOfStartOfReportingPeriod"].iloc[0]["Value"]).endswith("07-01"):
                quarter, report_date = "Q2",  date(fy_year, 9, 30)
            elif str(data.loc[labels == "DateOfStartOfReportingPeriod"].iloc[0]["Value"]).endswith("10-01"):
                quarter, report_date = "Q3", date(fy_year, 12, 31)
            elif str(data.loc[labels == "DateOfStartOfReportingPeriod"].iloc[0]["Value"]).endswith("01-01"):
                quarter, report_date = "Q4", date(fy_year, 3, 31)
            else:
                return None
        else:
            quarter, report_date = quarter_map[reporting_quarter]
    else:
        quarter, report_date = "", date(fy_year, 3, 31)

    return {
        "financial_year": fy_year,
        "quarter": quarter,
        "report_date": report_date
    }
# =========================================================
# DATABASE CONFIG
# =========================================================

# DB_USER = "postgres"
# DB_PASSWORD = "htKgJYQlRWe79FVgE1Fg"
# DB_HOST = "decodealpha.cir4amqeix27.us-east-1.rds.amazonaws.com"
# DB_PORT = "5432"
# DB_NAME = "decodealpha"

DB_USER = "postgres"
DB_PASSWORD = "decodeup123"
DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "alphadecodetest"


engine = create_engine(
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
    pool_pre_ping=True,
)

Session = sessionmaker(bind=engine)


# =========================================================
# EXCEL FILE PATH
# =========================================================

EXCEL_MAPPING_FILE = "/home/decodeup/Documents/alphaDecodeDataUpload/Detailed_page_keys.xlsx"

# =========================================================
# SHEET CONFIGURATION
# =========================================================

SHEET_TABLE_MAP = {
    "balancesheet": {
        "sheet_name": "Balance-sheet",
        "table_name": "balance_sheet_financials"
    },
    "pnl": {
        "sheet_name": "ProfitLoss",
        "table_name": "pnl_financials"
    },
    "cashflow": {
        "sheet_name": "Cashflow",
        "table_name": "cashflow_financials"
    }
}

# =========================================================
# LOAD SYMBOLS
# =========================================================


def load_symbol_mapping():
    """
    Load symbols table.

    Assumption:
    symbols table has:
        id
        symbol
    """

    query = text("""
        SELECT id, symbol
        FROM symbols
    """)

    with engine.connect() as conn:
        rows = conn.execute(query)

        return {
            row.symbol.upper(): row.id
            for row in rows
        }


SYMBOL_MAP = load_symbol_mapping()
print(SYMBOL_MAP)
print(f"Loaded {len(SYMBOL_MAP)} symbols")

# =========================================================
# LOAD EXCEL MAPPINGS
# =========================================================


def normalize_sector_name(col_name):
    col_name = str(col_name).strip().lower()

    if col_name.startswith("banking"):
        return "Banking"

    if col_name.startswith("life insurance"):
        return "Life insurance"

    if col_name.startswith("gen insurance") or col_name.startswith("general insurance"):
        return "Gen Insurance"

    if col_name.startswith("non-banking") or col_name.startswith("non banking"):
        return "Non-Banking"

    if col_name.startswith("industries") or col_name.startswith("industry"):
        return "Industries"

    if col_name.startswith("other"):
        return "Other"

    return None


CASHFLOW_SHEET_MAP = {
    "Banking": "Cashflow-Banking",
    "Life insurance": "Cashflow-Life Insurance",
    "Gen Insurance": "Cashflow-Gen Insurance",
    "Non-Banking": "Cashflow-Non Banking",
    "Industries": "Cashflow-Indutry",
    "Other": "Cashflow-Other"
}


def load_cashflow_mapping():

    mapping = {}

    for sector, sheet_name in CASHFLOW_SHEET_MAP.items():

        df = pd.read_excel(
            EXCEL_MAPPING_FILE,
            sheet_name=sheet_name
        )

        df = df.where(pd.notna(df), None)

        value_column = df.columns[1]

        mapping[sector] = {}

        for _, row in df.iterrows():

            parameter = row["Parameter"]
            value = row[value_column]

            if parameter and value and str(value).strip() != "-":
                mapping[sector][parameter] = str(value).strip()

    return mapping

def load_sheet_mapping(sheet_name):

    df = pd.read_excel(
        EXCEL_MAPPING_FILE,
        sheet_name=sheet_name
    )

    df = df.where(pd.notna(df), None)

    mapping = {}

    for col in df.columns:

        if str(col).strip() == "Parameter":
            continue

        sector = normalize_sector_name(col)

        if sector is None:
            continue

        mapping[sector] = {}

        for _, row in df.iterrows():

            parameter = row["Parameter"]
            value = row[col]

            if parameter and value and str(value).strip() != "-":
                mapping[sector][parameter] = str(value).strip()

    return mapping

# =========================================================
# BULK INSERT
# =========================================================


def bulk_insert(table_name, rows):
    """
    Fast bulk insert using execute_values
    """

    if not rows:
        return

    raw_conn = engine.raw_connection()

    try:
        cursor = raw_conn.cursor()

        query = f"""
            INSERT INTO {table_name} (
                symbol_id,
                statement_type,
                date,
                frequency,
                period,
                metric_name,
                metric_value,
                year
            )
            VALUES %s
            ON CONFLICT (
                symbol_id,
                statement_type,
                frequency,
                date,
                metric_name
            )
            DO UPDATE SET
                metric_value = EXCLUDED.metric_value,
                period = EXCLUDED.period,
                year = EXCLUDED.year,
                updated_at = NOW()
            WHERE
                {table_name}.metric_value IS DISTINCT FROM EXCLUDED.metric_value
                OR {table_name}.period IS DISTINCT FROM EXCLUDED.period
                OR {table_name}.year IS DISTINCT FROM EXCLUDED.year
        """

        values = [
            (
                row["symbol_id"],
                row["statement_type"],
                row["date"],
                row["frequency"],
                row["period"],
                row["metric_name"],
                row["metric_value"],
                row["year"]
            )
            for row in rows
        ]

        execute_values(cursor, query, values, page_size=10000)

        raw_conn.commit()

    except Exception as e:
        raw_conn.rollback()
        print(f"Bulk insert failed: {e}")

    finally:
        cursor.close()
        raw_conn.close()

# =========================================================
# FIND COMPANY FILE
# =========================================================


def find_company_file(statement_folder, frequency, symbol):
    """
    Priority:
        1. consolidated
        2. standalone
    """

    consolidated_path = os.path.join(
        ROOT_DIR,
        "consolidated",
        statement_folder,
        frequency,
        f"{symbol}.csv"
    )

    standalone_path = os.path.join(
        ROOT_DIR,
        "standalone",
        statement_folder,
        frequency,
        f"{symbol}.csv"
    )

    if os.path.exists(consolidated_path):
        return consolidated_path, "consolidated"

    if os.path.exists(standalone_path):
        return standalone_path, "standalone"

    return None, None

# =========================================================
# PARSE DATE
# =========================================================


def parse_date(value):
    """
    Convert CSV date to python date
    """

    try:
        return pd.to_datetime(value).date()
    except:
        return None

# =========================================================
# CLEAN NUMERIC
# =========================================================


def clean_numeric(value):
    """
    Convert values safely
    """

    if pd.isna(value):
        return None

    try:
        if isinstance(value, str):
            value = value.replace(",", "")
            value = value.strip()

            if value == "":
                return None

        return float(value)

    except:
        return None

# =========================================================
# PROCESS CSV FILE
# =========================================================

def parse_period_folder(folder_name):

    year = int(folder_name[:4])

    suffix = folder_name[4:]

    quarterly_periods = {
        "QJ",
        "QM",
        "QS",
        "QD"
    }

    if suffix in quarterly_periods:
        frequency = "quarterly"

    elif suffix == "M12":
        frequency = "yearly"

    else:
        return None
    return {
        "year": year,
        "frequency": frequency,
        "period": suffix
    }

special_company_types = {
    "IMPAL": "Industries",
    "SPIC": "Industries",
    "SUNDARMFIN": "Non-Banking",
    "WHEELS": "Industries",
    "BHARATRAS": "Industries",
    "KARURVYSYA": "Banking",
    "SUPRIYA": "Industries",
    "COHANCE": "Industries",
    "AZAD": "Industries",
}
missing_rows = []
def process_csv(
    csv_path,
    statement_type,
    frequency,
    period,
    year,
    mapping,
    table_name,
    symbol,
    symbol_id
):

    print(f"Processing: {symbol} -> {csv_path}")

    try:
        df = pd.read_csv(csv_path)


    except Exception as e:
        print(f"CSV read failed: {csv_path}")
        print(e)
        return False
    if df.empty:
        return False
    xbrl_rows = df[df["Label"] == "XbrlUrl"]["Value"].values.tolist()

    if not xbrl_rows:
        raise Exception("XbrlUrl missing")

    xbrl_url = xbrl_rows[0]
    file_name = str(xbrl_url).split("/")[-1]
    if isinstance(xbrl_url, float) and math.isnan(xbrl_url):
        if symbol in special_company_types:
            sector = special_company_types[symbol]
        else:
            return False

    elif "IFGI" in file_name:
        sector = "Gen Insurance"

    elif "NonBanking" in file_name or "NBFC" in file_name:
        sector = "Non-Banking"

    elif "IFLI" in file_name:
        sector = "Life insurance"

    elif (
            "IFBanking" in file_name
            or file_name.startswith("Banking")
    ):
        sector = "Banking"

    elif (
            "indas" in file_name.lower()
            or "Main_Ind_As" in file_name
    ):
        sector = "Industries"

    else:
        sector = "Other"


    columns = [str(c).strip() for c in df.columns]
    df.columns = columns
    metadata = extract_financial_metadata(df, frequency)
    if metadata is None:
        print(f"Financial metadata missing: {csv_path}")
        return False
    all_rows = []
    if sector is None:
        return False
    sector_mapping = mapping.get(sector)

    if not sector_mapping:
        print(f"No mapping found for sector={sector}")
        return False

    for metric_name, record in sector_mapping.items():

        metric_value = get_metric_value(df, record)

        if metric_value is None:
            if metric_name not in ["QoQ Revenue Growth %", "Employee Cost %", "Other Cost %", "Material Cost %",
                               "Operating Profit Margin %", 'QoQ Operating Profit Growth %', "Net Profit Margin %",
                               "QoQ Net Profit Growth %", "Benefits Ratio %", "Commission %", "Finance Cost %",
                               "ECL Provision %", "Effective Tax Rate %", "EPS Growth%"]:
                missing_rows.append([symbol, metric_name, record, statement_type, frequency, period, year])
            print(f"missing key in csv {csv_path}, {record}")
            continue

        all_rows.append({
            "symbol_id": symbol_id,
            "statement_type": statement_type,
            "date": metadata["report_date"],
            "frequency": frequency,
            "period": metadata["quarter"],
            "metric_name": metric_name,
            "metric_value": metric_value,
            "year": metadata["financial_year"]
        })
    bulk_insert(table_name, all_rows)
    return True



# =========================================================
# MAIN ETL
# =========================================================


ROOT_DIR = "/home/decodeup/Documents/alphaDecodeDataUpload"

DATA_SOURCES = {
    "consolidated": os.path.join(ROOT_DIR, "all_consolidated"),
    "standalone": os.path.join(ROOT_DIR, "all_standalone"),
}


STATEMENT_CONFIG = {
    # "BalanceSheet": {
    #     "sheet_name": "Balance-sheet",
    #     "table_name": "balance_sheet_financials"
    # },
    # "ProfitLoss": {
    #     "sheet_name": "ProfitLoss",
    #     "table_name": "pnl_financials"
    # },
    "CashFlow": {
        "sheet_name": None,
        "table_name": "cashflow_financials"
    }
}

import re
import math


def get_label_value(df, label):

    rows = df[df["Label"] == label]

    if rows.empty:

        return None

    try:
        return float(rows.iloc[0]["Value"])
    except:
        print(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        return None


def get_metric_value(df, expression):

    if expression is None:
        return None

    expression = str(expression).strip()

    if not expression:
        return None

    expression = expression.replace("\n", " ")
    expression = expression.replace("\r", " ")
    expression = expression.replace("÷", "/")


    expression = re.sub(r"\s+", " ", expression)

    expression = expression.replace("\n", " ")
    expression = expression.replace("\r", " ")

    # Normalize mathematical operators
    expression = (
        expression
        .replace("÷", "/")
        .replace("×", "*")
        .replace("−", "-")  # Unicode minus
        .replace("–", "-")  # En dash
        .replace("—", "-")  # Em dash
    )

    expression = re.sub(r"\s+", " ", expression).strip()
    print("??????????????",expression)

    tokens = set(
        re.findall(
            r"[A-Za-z][A-Za-z0-9_]*",
            expression
        )
    )
    values = {}

    missing_labels = []
    for token in tokens:
        # print("token", token)

        value = get_label_value(df, token)
        print("%%%%%%", value)
        if value is None:
            missing_labels.append(token)
            value = 0

        values[token] = value

    if missing_labels:
        print(
            f"Missing labels in formula [{expression}] -> "
            f"{missing_labels}"
        )

    safe_globals = {
        "__builtins__": {},
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "math": math
    }

    try:

        result = eval(
            expression,
            safe_globals,
            values
        )

        if result is None:
            print("helllooo")
            return None

        if isinstance(result, (int, float)):
            if math.isinf(result):
                return None

            if math.isnan(result):
                return None

        return float(result)

    except ZeroDivisionError:

        print(f"Division by zero in formula: {expression}")

        return None

    except Exception as e:

        print(
            f"Formula evaluation failed: {expression}"
        )

        print(e)

        return None

def run_etl():

    total_inserted = 0

    for statement_folder, config in STATEMENT_CONFIG.items():

        if statement_folder == "CashFlow":
            mapping = load_cashflow_mapping()
        else:
            mapping = load_sheet_mapping(config["sheet_name"])

        table_name = config["table_name"]

        print(f"\nProcessing {statement_folder}")

        # consolidated first
        for statement_type in ["consolidated", "standalone"]:

            base_path = DATA_SOURCES[statement_type]

            statement_path = os.path.join(
                base_path,
                statement_folder
            )
    #
            if not os.path.exists(statement_path):
                continue

            period_folders = os.listdir(statement_path)

            for period_folder in period_folders:
                metadata = parse_period_folder(period_folder)
                if metadata is None:
                    continue
                period_path = os.path.join(statement_path,period_folder)
                if not os.path.isdir(period_path):
                    continue
                csv_files = [
                    f for f in os.listdir(period_path)
                    if f.endswith(".csv")
                ]
                for csv_file in csv_files:

                    symbol = os.path.splitext(csv_file)[0].upper()

                    if symbol not in SYMBOL_MAP:
                        continue

                    symbol_id = SYMBOL_MAP[symbol]

                    csv_path = os.path.join(
                        period_path,
                        csv_file
                    )
    #
                    try:
                        xyz = process_csv(
                            csv_path=csv_path,
                            statement_type=statement_type,
                            frequency=metadata["frequency"],
                            period=metadata["period"],
                            year=metadata["year"],
                            mapping=mapping,
                            table_name=table_name,
                            symbol=symbol,
                            symbol_id=symbol_id
                        )

                        total_inserted += 1
                        if xyz == False:
                            print(f"FAILED xyz: {csv_path}")
                        else:
                            print("success")
                    except Exception as e:
                        print(f"FAILED: {csv_path}")
                        print(e)
    #
    # print(f"\nCompleted. Processed: {total_inserted}")
# =========================================================
# ENTRYPOINT
# =========================================================



if __name__ == "__main__":
    run_etl()


df2 = pd.DataFrame(missing_rows, columns=["symbol", "record", "key name", "statement_type", "frequency", "period", "year"])

df2.to_csv("missing_data_cashflow.csv")