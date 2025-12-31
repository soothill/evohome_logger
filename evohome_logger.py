#!/usr/bin/env python3
import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from evohomeclient import EvohomeClient
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
IP_CACHE_FILE = DATA_DIR / "influx_ip_cache.json"
OFFLINE_BUFFER_FILE = DATA_DIR / "offline_buffer.json"
TOKEN_CACHE_FILE = DATA_DIR / "evohome_token.json"
DEFAULT_TIMEOUT_MS = int(os.environ.get("HTTP_TIMEOUT_MS", "10000"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evohome to InfluxDB collector")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run connectivity checks only (Evohome login + InfluxDB health), no writes",
    )
    return parser.parse_args()


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("evohome_logger")
    if logger.handlers:
        return logger

    level_name = os.environ.get("LOG_LEVEL")
    if not level_name and os.environ.get("DEBUG", "").lower() in {"1", "true", "yes", "on"}:
        level_name = "DEBUG"
    level = getattr(logging, (level_name or "INFO").upper(), logging.INFO)
    logger.setLevel(level)
    handlers: List[logging.Handler] = []

    syslog_path = Path("/dev/log")
    if syslog_path.exists():
        try:
            handlers.append(logging.handlers.SysLogHandler(address=str(syslog_path)))
        except OSError:
            pass

    handlers.append(logging.StreamHandler(sys.stdout))
    formatter = logging.Formatter("evohome_logger: %(levelname)s %(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.propagate = False
    logger.debug("Logger initialized at level %s", logging.getLevelName(logger.level))
    return logger


def atomic_write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    tmp_path.replace(path)


def try_write_json(path: Path, payload: Dict, logger: logging.Logger) -> bool:
    try:
        atomic_write_json(path, payload)
        return True
    except OSError as exc:  # noqa: BLE001
        logger.warning("Failed to persist %s: %s", path, exc)
        return False


def load_token_cache(logger: logging.Logger) -> Optional[Dict]:
    data = load_json(TOKEN_CACHE_FILE)
    if not data:
        return None
    expires_at = data.get("expires_at")
    if expires_at:
        try:
            if time.time() > float(expires_at) - 60:  # refresh 1 minute early
                logger.info("Cached Evohome token expired or near expiry; ignoring cached token")
                return None
        except (TypeError, ValueError):
            pass
    return data


def normalize_expiry(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        if isinstance(value, str):
            return float(value)
    except ValueError:
        return None
    try:
        if isinstance(value, datetime):
            return value.timestamp()
    except Exception:
        return None
    return None


def persist_token_cache(client: "EvohomeClient", logger: logging.Logger) -> None:
    token_payload: Dict = {}
    candidates = [
        "access_token",
        "refresh_token",
        "access_token_expires",
        "token_expires",
        "token_expiration",
        "session_id",
        "tokens",
    ]
    for key in candidates:
        if hasattr(client, key):
            token_payload[key] = getattr(client, key)

    if hasattr(client, "tokens") and isinstance(getattr(client, "tokens"), dict):
        token_payload.update(getattr(client, "tokens"))

    expires_source = (
        token_payload.get("access_token_expires")
        or token_payload.get("token_expires")
        or token_payload.get("token_expiration")
        or token_payload.get("expires_at")
    )
    expires_at = normalize_expiry(expires_source)
    if expires_at:
        token_payload["expires_at"] = expires_at

    if not token_payload:
        return

    try_write_json(TOKEN_CACHE_FILE, token_payload, logger)


def load_json(path: Path) -> Optional[Dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def resolve_influx_ip(hostname: str, logger: logging.Logger) -> Tuple[Optional[str], bool]:
    """Return (ip, from_cache)."""
    cached = load_json(IP_CACHE_FILE) or {}
    cached_ip = cached.get("ip") if cached.get("host") == hostname else None

    if not hostname:
        return cached_ip, True if cached_ip else False

    try:
        ip = socket.getaddrinfo(hostname, None)[0][4][0]
        if ip != cached_ip:
            try_write_json(IP_CACHE_FILE, {"host": hostname, "ip": ip}, logger)
        return ip, False
    except socket.gaierror as exc:
        logger.error("DNS lookup failed for %s: %s", hostname, exc)
        if cached_ip:
            logger.warning("Using cached InfluxDB IP %s", cached_ip)
            return cached_ip, True
    return None, False


def build_influx_endpoint(base_url: str, resolved_ip: Optional[str]) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (url, host_header, hostname)."""
    parsed = urlparse(base_url)
    hostname = parsed.hostname
    if resolved_ip and hostname and resolved_ip != hostname:
        netloc = resolved_ip
        if parsed.port:
            netloc = f"{resolved_ip}:{parsed.port}"
        rebuilt = parsed._replace(netloc=netloc)
        return rebuilt.geturl(), hostname, hostname
    return base_url, None, hostname


def get_config(logger: logging.Logger) -> Dict:
    required_env = [
        "EVOHOME_USERNAME",
        "EVOHOME_PASSWORD",
        "INFLUX_URL",
        "INFLUX_BUCKET",
        "INFLUX_ORG",
        "INFLUX_TOKEN",
    ]
    missing = [var for var in required_env if not os.environ.get(var)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    return {
        "username": os.environ["EVOHOME_USERNAME"],
        "password": os.environ["EVOHOME_PASSWORD"],
        "influx_url": os.environ["INFLUX_URL"],
        "influx_bucket": os.environ["INFLUX_BUCKET"],
        "influx_org": os.environ["INFLUX_ORG"],
        "influx_token": os.environ["INFLUX_TOKEN"],
        "verify_tls": os.environ.get("INFLUX_VERIFY_TLS", "true").lower() != "false",
        "location_idx": int(os.environ.get("EVOHOME_LOCATION_INDEX", "0")),
    }


def build_evo_client(config: Dict, logger: logging.Logger) -> EvohomeClient:
    tokens = load_token_cache(logger)
    client = None

    base_kwargs: Dict = {}
    try:
        import inspect

        params = inspect.signature(EvohomeClient).parameters
        if "debug" in params:
            base_kwargs["debug"] = False
        token_kwargs = {}
        if tokens:
            for key in ["access_token", "refresh_token", "access_token_expires", "token_expires", "token_expiration", "tokens", "session_id"]:
                if key in params and key in tokens:
                    token_kwargs[key] = tokens[key]
            if "tokens" in params and "tokens" not in token_kwargs and tokens:
                token_kwargs["tokens"] = tokens
        if token_kwargs:
            try:
                client = EvohomeClient(config["username"], config["password"], **base_kwargs, **token_kwargs)
                logger.info("Initialized Evohome client with cached token data")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to initialize Evohome client with cached tokens: %s", exc)
        if not client:
            client = EvohomeClient(config["username"], config["password"], **base_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to create Evohome client: %s", exc)
        raise

    return client


def create_influx_client(url: str, token: str, org: str, verify_tls: bool, host_header: Optional[str]) -> InfluxDBClient:
    headers = {"User-Agent": "evohome-logger/1.0"}
    if host_header:
        headers["Host"] = host_header
    return InfluxDBClient(
        url=url,
        token=token,
        org=org,
        timeout=DEFAULT_TIMEOUT_MS,
        verify_ssl=verify_tls,
        default_headers=headers,
    )


def safe_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_zone_meta(installation: Dict) -> Dict[str, Dict]:
    meta: Dict[str, Dict] = {}
    gateways = installation.get("gateways") or []
    for gateway in gateways:
        systems = gateway.get("temperatureControlSystems") or []
        for system in systems:
            system_id = system.get("systemId") or system.get("systemID")
            zones = system.get("zones") or []
            for zone in zones:
                zone_id = str(zone.get("zoneId") or zone.get("zoneID") or zone.get("id") or "")
                if not zone_id:
                    continue
                meta[zone_id] = {
                    "system_id": system_id,
                    "heat_demand": zone.get("heatDemand"),
                    "setpoint_status": zone.get("setpointStatus") or {},
                    "temperature_status": zone.get("temperatureStatus") or {},
                    "active_faults": zone.get("activeFaults") or [],
                    "zone_type": zone.get("zoneType"),
                }
    return meta


def extract_dhw(installation: Dict) -> List[Dict]:
    dhw_points: List[Dict] = []
    gateways = installation.get("gateways") or []
    for gateway in gateways:
        systems = gateway.get("temperatureControlSystems") or []
        for system in systems:
            system_id = system.get("systemId") or system.get("systemID")
            dhw = system.get("dhw") or {}
            if not dhw:
                continue
            dhw_state = dhw.get("stateStatus") or {}
            temp_status = dhw.get("temperatureStatus") or {}
            dhw_points.append(
                {
                    "system_id": system_id,
                    "status": dhw_state.get("status") or dhw_state.get("mode"),
                    "temperature": safe_float(temp_status.get("temperature")),
                    "is_available": dhw_state.get("isAvailable"),
                    "mode": dhw_state.get("mode"),
                }
            )
    return dhw_points


def build_points(temperatures: List[Dict], installation: Dict, logger: logging.Logger) -> List[Point]:
    timestamp = datetime.now(timezone.utc)
    zone_meta = extract_zone_meta(installation)
    points: List[Point] = []

    for zone in temperatures or []:
        zone_id = str(zone.get("id") or zone.get("zoneId") or zone.get("zoneID") or zone.get("name") or "unknown")
        meta = zone_meta.get(zone_id, {})
        point = Point("evohome_zone").tag("zone_id", zone_id)

        if zone.get("name"):
            point = point.tag("zone", str(zone.get("name")))
        if meta.get("system_id"):
            point = point.tag("system_id", str(meta.get("system_id")))
        if meta.get("zone_type"):
            point = point.tag("zone_type", str(meta.get("zone_type")))

        temp_value = safe_float(zone.get("temp"))
        if temp_value is None:
            temp_value = safe_float((meta.get("temperature_status") or {}).get("temperature"))
        if temp_value is not None:
            point = point.field("temperature", temp_value)

        setpoint = safe_float(zone.get("setpoint"))
        if setpoint is None:
            setpoint = safe_float((meta.get("setpoint_status") or {}).get("targetHeatTemperature"))
        if setpoint is not None:
            point = point.field("setpoint", setpoint)

        heat_demand = safe_float(zone.get("heat_demand"))
        if heat_demand is None:
            heat_demand = safe_float(meta.get("heat_demand"))
        if heat_demand is not None:
            point = point.field("heat_demand", heat_demand)

        status = zone.get("status") or zone.get("mode") or (meta.get("setpoint_status") or {}).get("status")
        if status:
            point = point.field("status", str(status))

        faults = meta.get("active_faults") or []
        if isinstance(faults, list):
            point = point.field("fault_count", len(faults))

        point = point.time(timestamp)
        if point.to_line_protocol():
            points.append(point)

    for dhw in extract_dhw(installation):
        point = Point("evohome_dhw").tag("system_id", str(dhw.get("system_id"))).time(timestamp)
        if dhw.get("status"):
            point = point.field("status", str(dhw.get("status")))
        if dhw.get("mode"):
            point = point.field("mode", str(dhw.get("mode")))
        temp_value = safe_float(dhw.get("temperature"))
        if temp_value is not None:
            point = point.field("temperature", temp_value)
        availability = dhw.get("is_available")
        if availability is not None:
            point = point.field("is_available", bool(availability))
        if point.to_line_protocol():
            points.append(point)

    logger.info("Prepared %d points", len(points))
    return points


def points_to_lines(records: List[Point]) -> List[str]:
    lines: List[str] = []
    for rec in records:
        if not rec:
            continue
        line = rec.to_line_protocol()
        if line:
            lines.append(line)
    return lines


def load_offline_records() -> List[str]:
    data = load_json(OFFLINE_BUFFER_FILE) or {}
    return data.get("records", []) if isinstance(data, dict) else []


def persist_offline_records(records: List[str], logger: logging.Logger) -> None:
    if not records:
        return
    if try_write_json(OFFLINE_BUFFER_FILE, {"records": records}, logger):
        logger.warning("Cached %d records locally (offline buffer)", len(records))


def write_points(records: List[Point], influx: InfluxDBClient, bucket: str, org: str, logger: logging.Logger) -> bool:
    previous = load_offline_records()
    payload = previous + points_to_lines(records)

    if not payload:
        logger.info("No records to write")
        return True

    try:
        write_api = influx.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=bucket, org=org, record=payload)
        if OFFLINE_BUFFER_FILE.exists():
            try:
                OFFLINE_BUFFER_FILE.unlink()
            except OSError as exc:  # noqa: BLE001
                logger.warning("Failed to clear offline buffer: %s", exc)
        logger.info("Successfully wrote %d records (including %d from offline cache)", len(payload), len(previous))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write to InfluxDB: %s", exc)
        persist_offline_records(payload, logger)
        return False


def fetch_evohome_data(client: EvohomeClient, location_idx: int, logger: logging.Logger) -> Tuple[List[Dict], Dict]:
    try:
        temperatures = client.temperatures(force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch temperatures: %s", exc)
        temperatures = []

    def log_installation_debug(payload) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            if isinstance(payload, list):
                logger.debug("Installation payload: list length=%d", len(payload))
                if payload:
                    sample = payload[0]
                    if isinstance(sample, dict):
                        logger.debug("Installation sample keys: %s", list(sample.keys()))
            elif isinstance(payload, dict):
                logger.debug("Installation payload: dict keys=%s", list(payload.keys()))
            else:
                logger.debug("Installation payload type: %s", type(payload))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Unable to log installation payload details: %s", exc)

    def fetch_installation() -> Dict:
        candidates = [
            ("full_installation", True),
            ("installation_info", True),
            ("installation", True),
            ("installation", False),
        ]
        for name, callable_only in candidates:
            method = getattr(client, name, None)
            if callable_only and not callable(method):
                logger.debug("%s not callable or missing", name)
                continue
            try:
                payload = method() if callable_only else method
                log_installation_debug(payload)
                if payload:
                    logger.debug("Using installation payload from %s", name)
                    return payload
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s failed: %s", name, exc)
        return {}

    installation_data = fetch_installation()
    installation = {}
    if isinstance(installation_data, list) and installation_data:
        try:
            installation = installation_data[location_idx]
        except (IndexError, TypeError):
            installation = installation_data[0]
    elif isinstance(installation_data, dict):
        installation = installation_data
    else:
        logger.warning("Unexpected installation payload type: %s", type(installation_data))

    if not installation:
        logger.warning("Proceeding without installation details; some tags may be missing")

    return temperatures, installation if installation else {}


def check_connectivity(config: Dict, logger: logging.Logger) -> bool:
    evo_ok = False
    influx_ok = False

    try:
        evo_client = build_evo_client(config, logger)
        evo_client.temperatures(force_refresh=True)
        persist_token_cache(evo_client, logger)
        evo_ok = True
        logger.info("Evohome connectivity: OK")
    except Exception as exc:  # noqa: BLE001
        logger.error("Evohome connectivity failed: %s", exc)

    parsed_influx = urlparse(config["influx_url"])
    resolved_ip, from_cache = resolve_influx_ip(parsed_influx.hostname or "", logger)
    if resolved_ip and from_cache:
        logger.info("Using cached InfluxDB IP for %s", parsed_influx.hostname or "provided URL")
    if parsed_influx.hostname and not resolved_ip:
        logger.error("Unable to resolve InfluxDB host %s", parsed_influx.hostname)
    else:
        resolved_url, host_header, _ = build_influx_endpoint(config["influx_url"], resolved_ip)
        try:
            influx_client = create_influx_client(
                url=resolved_url,
                token=config["influx_token"],
                org=config["influx_org"],
                verify_tls=config["verify_tls"],
                host_header=host_header,
            )
            health = influx_client.health()
            status = getattr(health, "status", None) or (health.get("status") if isinstance(health, dict) else None)
            if status and str(status).lower() in {"pass", "ok", "healthy"}:
                influx_ok = True
                logger.info("InfluxDB connectivity: OK (%s)", status)
            else:
                logger.error("InfluxDB health check failed: %s", status or "unknown status")
        except Exception as exc:  # noqa: BLE001
            logger.error("InfluxDB connectivity failed: %s", exc)

    if evo_ok and influx_ok:
        logger.info("Connectivity check succeeded")
    else:
        logger.error("Connectivity check failed (Evohome OK=%s, InfluxDB OK=%s)", evo_ok, influx_ok)
    return evo_ok and influx_ok


def main() -> None:
    logger = setup_logger()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.debug("DATA_DIR resolved to %s", DATA_DIR)

    args = parse_args()
    config = get_config(logger)

    if args.check:
        success = check_connectivity(config, logger)
        sys.exit(0 if success else 1)

    try:
        evo_client = build_evo_client(config, logger)
    except Exception:
        sys.exit(1)

    temperatures, installation = fetch_evohome_data(evo_client, config["location_idx"], logger)
    persist_token_cache(evo_client, logger)

    parsed_influx = urlparse(config["influx_url"])
    resolved_ip, from_cache = resolve_influx_ip(parsed_influx.hostname or "", logger)
    if resolved_ip and from_cache:
        logger.info("Using cached InfluxDB IP for %s", parsed_influx.hostname or "provided URL")
    if parsed_influx.hostname and not resolved_ip:
        logger.error("Unable to resolve InfluxDB host %s; will cache data locally", parsed_influx.hostname)
    resolved_url, host_header, _ = build_influx_endpoint(config["influx_url"], resolved_ip)

    influx_client: Optional[InfluxDBClient] = None
    if resolved_url and resolved_ip:
        try:
            influx_client = create_influx_client(
                url=resolved_url,
                token=config["influx_token"],
                org=config["influx_org"],
                verify_tls=config["verify_tls"],
                host_header=host_header,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to create InfluxDB client: %s", exc)

    points = build_points(temperatures, installation, logger)

    if not influx_client:
        logger.error("InfluxDB client unavailable; caching %d records", len(points))
        persist_offline_records(load_offline_records() + points_to_lines(points), logger)
        sys.exit(1)

    success = write_points(points, influx_client, config["influx_bucket"], config["influx_org"], logger)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
