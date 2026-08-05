import base64
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape

import pandas as pd
import pyodbc
import requests

ACCOUNT = os.getenv("NETSUITE_ACCOUNT", "5568309_SB2")
SOAP_VERSION = os.getenv("NETSUITE_SOAP_VERSION", "2025_1")
SQL_SCHEMA = os.getenv("AZURE_SQL_SCHEMA", "dbo")
NETSUITE_URL = os.getenv(
    "NETSUITE_URL",
    "https://5568309-sb2.suitetalk.api.netsuite.com"
    f"/services/NetSuitePort_{SOAP_VERSION}",
)
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "300"))
SQL_CONNECTION_TIMEOUT_SECONDS = int(os.getenv("SQL_CONNECTION_TIMEOUT_SECONDS", "30"))
INSERT_BATCH_SIZE = int(os.getenv("INSERT_BATCH_SIZE", "5000"))

FILES = [
    {"internal_id": "198014", "expected_filename": "Adaptive Integration - Item Master.csv", "table_name": "NS_Adaptive_ItemMaster"},
    {"internal_id": "198414", "expected_filename": "Adaptive Integration- SEI Inventory Report.csv", "table_name": "NS_SEI_InventoryReport"},
    {"internal_id": "209279", "expected_filename": "Adaptive Integration - Item Master updated Inactive Items (monthly).csv", "table_name": "NS_ItemMaster_InactiveItems"},
    {"internal_id": "198314", "expected_filename": "Adaptive Integration- Item Receipts CurrMonthToDate.csv", "table_name": "NS_ItemReceipts_CurrentMonth"},
    {"internal_id": "198214", "expected_filename": "Adaptive Integration- Open Purchase Orders.csv", "table_name": "NS_OpenPurchaseOrders"},
    {"internal_id": "246895", "expected_filename": "RSMInventoryReportNonSerializedAllLocationsMM.csv", "table_name": "NS_RSMInventory_NonSerialized"},
    {"internal_id": "246896", "expected_filename": "RSMItemInventoryValuationbyLocationMM.csv", "table_name": "NS_RSMInventoryValuation_ByLocation"},
]


def required_setting(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Required application setting {name} is missing.")
    return value


def get_credentials() -> dict[str, str]:
    return {
        "consumer_key": required_setting("NETSUITE_CONSUMER_KEY"),
        "consumer_secret": required_setting("NETSUITE_CONSUMER_SECRET"),
        "token_id": required_setting("NETSUITE_TOKEN_ID"),
        "token_secret": required_setting("NETSUITE_TOKEN_SECRET"),
        "sql_server": required_setting("AZURE_SQL_SERVER"),
        "sql_database": required_setting("AZURE_SQL_DATABASE"),
        "sql_username": required_setting("AZURE_SQL_USERNAME"),
        "sql_password": required_setting("AZURE_SQL_PASSWORD"),
    }


def create_token_passport(credentials: dict[str, str]) -> tuple[str, str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(10)
    base_string = "&".join([ACCOUNT, credentials["consumer_key"], credentials["token_id"], nonce, timestamp])
    signing_key = f'{credentials["consumer_secret"]}&{credentials["token_secret"]}'
    signature = base64.b64encode(
        hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return nonce, timestamp, signature


def build_xml_payload(file_internal_id: str, credentials: dict[str, str]) -> str:
    nonce, timestamp, signature = create_token_passport(credentials)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:urn="urn:messages_{SOAP_VERSION}.platform.webservices.netsuite.com"
 xmlns:urn1="urn:core_{SOAP_VERSION}.platform.webservices.netsuite.com">
 <soapenv:Header>
  <urn:tokenPassport>
   <urn1:account>{escape(ACCOUNT)}</urn1:account>
   <urn1:consumerKey>{escape(credentials["consumer_key"])}</urn1:consumerKey>
   <urn1:token>{escape(credentials["token_id"])}</urn1:token>
   <urn1:nonce>{escape(nonce)}</urn1:nonce>
   <urn1:timestamp>{escape(timestamp)}</urn1:timestamp>
   <urn1:signature algorithm="HMAC-SHA256">{escape(signature)}</urn1:signature>
  </urn:tokenPassport>
  <urn:preferences><urn:warningAsError>false</urn:warningAsError></urn:preferences>
 </soapenv:Header>
 <soapenv:Body>
  <urn:get>
   <urn:baseRef xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:type="urn1:RecordRef" internalId="{escape(file_internal_id)}" type="file"/>
  </urn:get>
 </soapenv:Body>
</soapenv:Envelope>'''


def find_xml_text(root: ET.Element, element_name: str) -> str | None:
    node = root.find(f".//{{*}}{element_name}")
    return None if node is None else node.text


def raise_for_netsuite_error(root: ET.Element) -> None:
    fault = root.find(".//{*}Fault")
    if fault is not None:
        fault_string = find_xml_text(fault, "faultstring")
        detail_message = find_xml_text(fault, "message")
        raise RuntimeError(f"NetSuite SOAP error: {detail_message or fault_string or 'Unknown error'}")
    status = root.find(".//{*}status")
    if status is not None and status.get("isSuccess", "").lower() == "false":
        raise RuntimeError(
            f"NetSuite unsuccessful result. Code: {find_xml_text(root, 'code')}. "
            f"Message: {find_xml_text(root, 'message')}"
        )


def download_netsuite_file(file_internal_id: str, credentials: dict[str, str]) -> tuple[str, bytes]:
    response = requests.post(
        NETSUITE_URL,
        headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": "get"},
        data=build_xml_payload(file_internal_id, credentials).encode("utf-8"),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        logging.error("NetSuite response: %s", response.text[:5000])
        response.raise_for_status()
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError("NetSuite response was not valid XML.") from exc
    raise_for_netsuite_error(root)
    encoded_content = find_xml_text(root, "content")
    returned_filename = find_xml_text(root, "name")
    if not encoded_content:
        raise RuntimeError(f"NetSuite returned no content for file {file_internal_id}.")
    try:
        file_bytes = base64.b64decode("".join(encoded_content.split()), validate=True)
    except ValueError as exc:
        raise RuntimeError(f"File {file_internal_id} did not contain valid Base64 content.") from exc
    return returned_filename or f"netsuite_file_{file_internal_id}.csv", file_bytes


def read_csv_bytes(file_bytes: bytes, filename: str) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ["utf-8-sig", "utf-8", "cp1252", "latin-1"]:
        try:
            frame = pd.read_csv(
                io.BytesIO(file_bytes), encoding=encoding, dtype=str,
                keep_default_na=False, na_filter=False, low_memory=False,
                quotechar='"', doublequote=True, on_bad_lines="error",
            )
            logging.info("Parsed %s using %s.", filename, encoding)
            return frame
        except UnicodeDecodeError as exc:
            last_error = exc
        except pd.errors.ParserError as exc:
            raise RuntimeError(f"CSV parsing failed for {filename}: {exc}") from exc
    raise RuntimeError(f"Unable to determine encoding for {filename}.") from last_error


def clean_sql_identifier(value: Any) -> str:
    identifier = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]+", "_", str(value).strip().lstrip("\ufeff"))).strip("_")
    if not identifier:
        identifier = "UnnamedColumn"
    if identifier[0].isdigit():
        identifier = f"Column_{identifier}"
    return identifier[:128]


def make_columns_unique(columns: list[Any]) -> list[str]:
    output: list[str] = []
    used: dict[str, int] = {}
    for original in columns:
        base = clean_sql_identifier(original)
        key = base.lower()
        if key not in used:
            used[key] = 1
            final = base
        else:
            used[key] += 1
            suffix = f"_{used[key]}"
            final = base[:128-len(suffix)] + suffix
        output.append(final)
    return output


def prepare_dataframe(frame: pd.DataFrame, internal_id: str, filename: str) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = make_columns_unique(frame.columns.tolist())
    frame["_NetSuiteFileInternalID"] = internal_id
    frame["_NetSuiteSourceFilename"] = filename
    frame["_LoadedUTC"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for column in frame.columns:
        frame[column] = frame[column].fillna("").astype(str)
    return frame


def validate_identifier(identifier: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", identifier):
        raise ValueError(f"Unsafe SQL identifier: {identifier}")


def quote_identifier(identifier: str) -> str:
    validate_identifier(identifier)
    return f"[{identifier}]"


def create_sql_connection(credentials: dict[str, str]) -> pyodbc.Connection:
    server = credentials["sql_server"].strip()
    if server.lower().startswith("tcp:"):
        server = server[4:]
    if "," not in server:
        server = f"{server},1433"
    connection_string = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER=tcp:{server};DATABASE={credentials['sql_database']};"
        f"UID={credentials['sql_username']};PWD={credentials['sql_password']};"
        "Encrypt=yes;TrustServerCertificate=no;"
        f"Connection Timeout={SQL_CONNECTION_TIMEOUT_SECONDS};LongAsMax=Yes;"
    )
    return pyodbc.connect(connection_string, autocommit=False)


def determine_sql_type(frame: pd.DataFrame, column: str) -> str:
    series = frame[column].fillna("").astype(str)
    maximum = 1 if len(series) == 0 else max(int(series.str.len().max()), 1)
    return "NVARCHAR(MAX)" if maximum > 4000 else f"NVARCHAR({min(max(maximum + 50, 100), 4000)})"


def build_create_table_sql(frame: pd.DataFrame, schema: str, table: str) -> str:
    definitions = [f"{quote_identifier(c)} {determine_sql_type(frame, c)} NULL" for c in frame.columns]
    return f"CREATE TABLE {quote_identifier(schema)}.{quote_identifier(table)} ({', '.join(definitions)})"


def table_exists(connection: pyodbc.Connection, schema: str, table: str) -> bool:
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT CASE WHEN OBJECT_ID(?, 'U') IS NULL THEN 0 ELSE 1 END", f"{schema}.{table}")
        return bool(cursor.fetchone()[0])
    finally:
        cursor.close()


def row_count(connection: pyodbc.Connection, schema: str, table: str) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(f"SELECT COUNT_BIG(*) FROM {quote_identifier(schema)}.{quote_identifier(table)}")
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def insert_dataframe(connection: pyodbc.Connection, frame: pd.DataFrame, schema: str, table: str) -> None:
    columns = ", ".join(quote_identifier(c) for c in frame.columns)
    placeholders = ", ".join("?" for _ in frame.columns)
    sql = f"INSERT INTO {quote_identifier(schema)}.{quote_identifier(table)} ({columns}) VALUES ({placeholders})"
    cursor = connection.cursor()
    cursor.fast_executemany = True
    try:
        total = len(frame)
        for start in range(0, total, INSERT_BATCH_SIZE):
            end = min(start + INSERT_BATCH_SIZE, total)
            batch = frame.iloc[start:end]
            rows = [tuple(None if v is None else str(v) for v in row) for row in batch.itertuples(index=False, name=None)]
            cursor.executemany(sql, rows)
            connection.commit()
            logging.info("Inserted %,d of %,d rows into %s.%s.", end, total, schema, table)
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def drop_table_if_exists(connection: pyodbc.Connection, schema: str, table: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"IF OBJECT_ID(?, 'U') IS NOT NULL DROP TABLE {quote_identifier(schema)}.{quote_identifier(table)}",
            f"{schema}.{table}",
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def replace_sql_table(connection: pyodbc.Connection, frame: pd.DataFrame, destination: str) -> None:
    staging = f"{destination}_Stage_{uuid.uuid4().hex[:8]}"
    cursor = connection.cursor()
    try:
        cursor.execute(build_create_table_sql(frame, SQL_SCHEMA, staging))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()

    try:
        insert_dataframe(connection, frame, SQL_SCHEMA, staging)
        loaded = row_count(connection, SQL_SCHEMA, staging)
        if loaded != len(frame):
            raise RuntimeError(f"Staging row mismatch: expected {len(frame):,}, loaded {loaded:,}.")

        cursor = connection.cursor()
        try:
            if table_exists(connection, SQL_SCHEMA, destination):
                cursor.execute(f"DROP TABLE {quote_identifier(SQL_SCHEMA)}.{quote_identifier(destination)}")
            cursor.execute("EXEC sp_rename ?, ?", f"{SQL_SCHEMA}.{staging}", destination)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()

        final = row_count(connection, SQL_SCHEMA, destination)
        if final != len(frame):
            raise RuntimeError(f"Final row mismatch: expected {len(frame):,}, found {final:,}.")
        logging.info("Replaced %s.%s with %,d rows.", SQL_SCHEMA, destination, final)
    except Exception:
        try:
            drop_table_if_exists(connection, SQL_SCHEMA, staging)
        except Exception:
            logging.exception("Could not clean up staging table %s.%s.", SQL_SCHEMA, staging)
        raise


def process_file(connection: pyodbc.Connection, config: dict[str, str], credentials: dict[str, str]) -> dict[str, Any]:
    internal_id = config["internal_id"]
    destination = config["table_name"]
    logging.info("Processing file %s into %s.%s.", internal_id, SQL_SCHEMA, destination)
    filename, file_bytes = download_netsuite_file(internal_id, credentials)
    logging.info("Downloaded %s (%.2f MB).", filename, len(file_bytes) / 1024 / 1024)
    frame = prepare_dataframe(read_csv_bytes(file_bytes, filename), internal_id, filename)
    logging.info("Prepared %,d rows and %,d columns.", len(frame), len(frame.columns))
    replace_sql_table(connection, frame, destination)
    return {"internal_id": internal_id, "table_name": destination, "rows": len(frame)}


def run_load() -> None:
    credentials = get_credentials()
    logging.info("Available ODBC drivers: %s", pyodbc.drivers())
    if "ODBC Driver 18 for SQL Server" not in pyodbc.drivers():
        raise RuntimeError("ODBC Driver 18 for SQL Server is unavailable.")

    connection = create_sql_connection(credentials)
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            cursor.close()
        logging.info("Azure SQL connection successful.")

        for config in FILES:
            try:
                successes.append(process_file(connection, config, credentials))
            except Exception as exc:
                logging.exception("File %s failed.", config["internal_id"])
                failures.append({"internal_id": config["internal_id"], "table_name": config["table_name"], "error": str(exc)})
    finally:
        connection.close()

    for result in successes:
        logging.info("SUCCESS | %s | %s | %,d rows", result["internal_id"], result["table_name"], result["rows"])
    for failure in failures:
        logging.error("FAILED | %s | %s | %s", failure["internal_id"], failure["table_name"], failure["error"])
    if failures:
        raise RuntimeError(f"{len(failures)} file load(s) failed.")
