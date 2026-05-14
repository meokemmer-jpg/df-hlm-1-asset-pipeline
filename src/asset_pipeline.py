from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _df_common.pii_scrubber import PIIScrubber, scrub_audit_payload  # noqa: E402
from _df_common.welle_b2_patches import (  # noqa: E402
    K13PreActionVerifier,
    K16MutexGuard,
    MOCK_PREFIX,
    make_mock_url,
    make_provenance_envelope,
)

try:
    import structlog
except ImportError:  # pragma: no cover
    class _FallbackLogger:
        def __init__(self, name: str, ctx: dict[str, Any] | None = None) -> None:
            self.name = name
            self.ctx = ctx or {}

        def bind(self, **kwargs: Any) -> "_FallbackLogger":
            return _FallbackLogger(self.name, {**self.ctx, **kwargs})

        def _emit(self, level: str, event: str, **kwargs: Any) -> None:
            payload = {"level": level, "event": event, **self.ctx, **kwargs}
            logging.getLogger(self.name).log(getattr(logging, level.upper()), json.dumps(payload, sort_keys=True))

        def info(self, event: str, **kwargs: Any) -> None:
            self._emit("info", event, **kwargs)

        def warning(self, event: str, **kwargs: Any) -> None:
            self._emit("warning", event, **kwargs)

        def error(self, event: str, **kwargs: Any) -> None:
            self._emit("error", event, **kwargs)

    class _FallbackStructlog:
        @staticmethod
        def configure(**_: Any) -> None:
            return None

        @staticmethod
        def get_logger(name: str | None = None) -> _FallbackLogger:
            return _FallbackLogger(name or "df-hlm-1")

    structlog = _FallbackStructlog()  # type: ignore[assignment]


class PipelineError(RuntimeError):
    pass


class ValidationError(PipelineError):
    pass


class MutexActiveError(PipelineError):
    pass


K16_LOCK_DIR = Path("/tmp/df-hlm-1.lock")
K16_ENGINE_MARKER = "asset_pipeline.py"


@dataclass(frozen=True)
class Hotel:
    hotel_id: str
    name: str
    city: str
    canonical_url: str


@dataclass(frozen=True)
class Persona:
    persona_id: str
    name: str
    segment: str
    tone: str
    desire: str


@dataclass(frozen=True)
class GeneratedAsset:
    channel: str
    file_path: str
    digest: str


def configure_logging() -> Any:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if hasattr(structlog, "processors"):
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.JSONRenderer(),
            ]
        )
    return structlog.get_logger("df-hlm-1")


def load_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_flag(env: Mapping[str, str], key: str, expected: str = "1") -> bool:
    return env.get(key, "") == expected


def digest_for(*parts: str) -> str:
    return sha256("::".join(parts).encode("utf-8")).hexdigest()[:16]


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def lint_html_document(html: str, tailwind_anchor: str) -> list[str]:
    errors: list[str] = []
    lowered = html.lower()
    if "<html" not in lowered or "</html>" not in lowered:
        errors.append("missing-html-shell")
    if tailwind_anchor not in html:
        errors.append("missing-tailwind-anchor")
    if "provenance:" not in lowered:
        errors.append("missing-provenance")
    if "data-external-anchor=" not in html:
        errors.append("missing-external-anchor")
    return errors


def validate_allowed_domain(url: str, allowed_domains: set[str]) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def detect_running_process(pattern: str, current_pid: int | None = None) -> bool:
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return any(pid != str(current_pid or os.getpid()) for pid in pids)


@contextmanager
def directory_mutex(lock_dir: Path, stale_age_h: int, pgrep_pattern: str | None = None) -> Iterator[None]:
    marker = pgrep_pattern or K16_ENGINE_MARKER
    guard = K16MutexGuard(lock_dir=lock_dir, df_engine_marker=marker, stale_age_hours=float(stale_age_h))
    try:
        result = guard.acquire()
        if not result.acquired:
            raise MutexActiveError(f"K16-VETO: {result.reason}")
        yield
    finally:
        guard.release()


CHANNEL_TEMPLATES: dict[str, str] = {
    "landing_page": """<!doctype html><html><head><script src=\"{tailwind_anchor}\"></script><title>{hotel_name}</title></head><body class=\"bg-[{charcoal}] text-[{ivory}]\"><!-- provenance: {provenance_payload} --><main data-external-anchor=\"{canonical_url}\"><h1>{hotel_name}</h1><p>{persona_name}: {desire}</p><a href=\"{canonical_url}\">Book now</a></main></body></html>""",
    "email": """<!doctype html><html><head><script src=\"{tailwind_anchor}\"></script><title>{hotel_name} email</title></head><body class=\"bg-white text-[{charcoal}]\"><!-- provenance: {provenance_payload} --><article data-external-anchor=\"{canonical_url}\"><h2>{persona_name}</h2><p>Discover {hotel_name} in {city} with a {tone} voice.</p><a href=\"{canonical_url}\">Reserve your stay</a></article></body></html>""",
    "social_card": """<!doctype html><html><head><script src=\"{tailwind_anchor}\"></script><title>{hotel_name} social</title></head><body class=\"bg-[{gold}] text-[{charcoal}]\"><!-- provenance: {provenance_payload} --><section data-external-anchor=\"{canonical_url}\"><strong>{persona_name}</strong><p>{desire}</p><a href=\"{canonical_url}\">See the hotel</a></section></body></html>""",
    "retargeting_ad": """<!doctype html><html><head><script src=\"{tailwind_anchor}\"></script><title>{hotel_name} ad</title></head><body class=\"bg-[{teal}] text-[{ivory}]\"><!-- provenance: {provenance_payload} --><aside data-external-anchor=\"{canonical_url}\"><p>{hotel_name} for the {persona_name}</p><a href=\"{canonical_url}\">Complete your booking</a></aside></body></html>""",
}


class AssetPipeline:
    def __init__(self, config_path: Path, env: Mapping[str, str] | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.env = dict(env or os.environ)
        self.logger = configure_logging().bind(df_id=self.config["df_id"])
        self.hotels = [Hotel(**entry) for entry in self.config["hotels"]]
        self.personas = [Persona(**entry) for entry in self.config["personas"]]
        self.allowed_domains = set(self.config["k13_independent_ground_truth"]["allowed_domains"])
        self.tailwind_anchor = self.config["tailwind"]["cdn_anchor"]
        self.colors = self.config["brand"]["colors"]
        self.audit_log_path = Path(self.config["operations"]["audit_log_path"])
        self.pii_scrubber = PIIScrubber(enabled=True, kemmer_names_enabled=True)

    def override_enabled(self) -> bool:
        return env_flag(self.env, self.config["k14_human_override_decay"]["override_env_var"])

    def real_output_enabled(self) -> bool:
        gate = self.config["env_var_gating"]
        return env_flag(self.env, gate["real_output_env_var"], gate["real_output_env_value"])

    def resolve_mode(self) -> str:
        gate = self.config["env_var_gating"]
        if env_flag(self.env, gate["direct_mode_env_var"]):
            return "direct"
        return "real" if self.real_output_enabled() else "mock"

    def stop_requested(self) -> bool:
        return Path(self.config["operations"]["stop_flag_path"]).exists()

    def mutex(self) -> Iterator[None]:
        settings = self.config["k16_concurrent_spawn_mutex"]
        return directory_mutex(
            K16_LOCK_DIR,
            int(settings["lock_stale_age_h"]),
            K16_ENGINE_MARKER if settings["engine_pgrep_check"] else None,
        )

    def output_root(self) -> Path:
        base = Path(self.config["operations"]["output_path"])
        return base / self.resolve_mode() / self.config["operations"]["bundle_subdir"]

    def bundle_root(self) -> Path:
        run_key = digest_for(
            self.config["version"],
            self.resolve_mode(),
            ",".join(h.hotel_id for h in self.hotels),
            ",".join(p.persona_id for p in self.personas),
        )
        return self.output_root() / run_key

    def health_check(self) -> dict[str, Any]:
        degraded = self.resolve_mode() != "real"
        return {
            "mode": self.resolve_mode(),
            "degraded": degraded,
            "health_score": self.config["lose_coupling"]["LC5_health_check"]["health_check_degraded_score"] if degraded else 1.0,
            "stop_requested": self.stop_requested(),
            "real_output_enabled": self.real_output_enabled(),
            "hotels": len(self.hotels),
            "personas": len(self.personas),
        }

    def check_entropy_budget(self) -> bool:
        estimate = int(self.config["k15_entropy_budget"]["entropy_added_loc_estimate"])
        actual = len(Path(__file__).read_text(encoding="utf-8").splitlines())
        return actual <= estimate + 120

    def scrub_text_output(self, content: str) -> str:
        return self.pii_scrubber.scrub(content)

    def scrub_data_output(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.pii_scrubber.scrub_dict_recursive(data)

    def write_output_text(self, path: Path, content: str) -> None:
        atomic_write_text(path, self.scrub_text_output(content))

    def write_output_json(self, path: Path, data: dict[str, Any]) -> None:
        scrubbed = self.scrub_data_output(data)
        atomic_write_text(path, json.dumps(scrubbed, indent=2, sort_keys=True))

    def provenance(self, mode: str, source_hash: str | None = None) -> dict[str, Any]:
        return make_provenance_envelope(
            df_id="DF-HLM-1",
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            is_mock=mode == "mock",
            source_hash=source_hash,
        )

    def output_url(self, hotel: Hotel, persona: Persona, channel: str, mode: str) -> str:
        if mode != "mock":
            return hotel.canonical_url
        identifier = digest_for(hotel.hotel_id, persona.persona_id, channel)
        return make_mock_url(hotel.canonical_url, identifier)

    def verify_real_dispatch(self) -> None:
        verifier = K13PreActionVerifier(
            expected_env_tag="dev",
            expected_mount_pattern="/Users/make",
            blast_radius_class="state-only",
        )
        result = verifier.verify()
        if not result.ok:
            raise RuntimeError(f"K13-VETO: {result.failed_check}")

    def audit(self, event: str, **payload: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
        scrubbed_record = scrub_audit_payload(record)
        scrubbed_payload = {k: v for k, v in scrubbed_record.items() if k not in {"ts", "event"}}
        self.logger.info(str(scrubbed_record["event"]), **scrubbed_payload)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(scrubbed_record, sort_keys=True) + "\n")

    def preflight_validate(self) -> None:
        invalid = [hotel.hotel_id for hotel in self.hotels if not validate_allowed_domain(hotel.canonical_url, self.allowed_domains)]
        if invalid and not self.override_enabled():
            raise ValidationError(f"K13 blocked invalid domains: {','.join(invalid)}")
        if not self.check_entropy_budget() and not self.override_enabled():
            raise ValidationError("K15 entropy budget exceeded")

    def render(
        self,
        channel: str,
        hotel: Hotel,
        persona: Persona,
        canonical_url: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        template = CHANNEL_TEMPLATES[channel]
        output_url = canonical_url or hotel.canonical_url
        provenance_payload = json.dumps(provenance or {"df_id": "DF-HLM-1"}, sort_keys=True)
        return template.format(
            channel=channel,
            hotel_id=hotel.hotel_id,
            hotel_name=hotel.name,
            city=hotel.city,
            canonical_url=output_url,
            persona_id=persona.persona_id,
            persona_name=persona.name,
            tone=persona.tone,
            desire=persona.desire,
            provenance_payload=provenance_payload,
            tailwind_anchor=self.tailwind_anchor,
            **self.colors,
        )

    def generate_combo(self, hotel: Hotel, persona: Persona, mode: str, bundle_root: Path) -> dict[str, Any]:
        combo_key = digest_for(self.config["version"], hotel.hotel_id, persona.persona_id)
        combo_dir = bundle_root / combo_key
        if self.env.get("DF_HLM_1_FAIL_COMBO") == f"{hotel.hotel_id}:{persona.persona_id}":
            raise RuntimeError("injected combo failure")
        assets: list[GeneratedAsset] = []
        for channel in CHANNEL_TEMPLATES:
            channel_source_hash = digest_for(self.config["version"], hotel.hotel_id, persona.persona_id, channel)
            provenance = self.provenance(mode, source_hash=channel_source_hash)
            canonical_url = self.output_url(hotel, persona, channel, mode)
            content = self.render(channel, hotel, persona, canonical_url=canonical_url, provenance=provenance)
            lint_errors = lint_html_document(content, self.tailwind_anchor)
            if canonical_url not in content:
                lint_errors.append("missing-canonical-anchor")
            if lint_errors and not self.override_enabled():
                raise ValidationError(f"K12/K13 validation failed for {hotel.hotel_id}/{persona.persona_id}: {lint_errors}")
            if mode != "direct":
                file_path = combo_dir / f"{channel}.html"
                self.write_output_text(file_path, content)
                assets.append(GeneratedAsset(channel=channel, file_path=str(file_path), digest=digest_for(content)))
        manifest = {
            "hotel": asdict(hotel),
            "persona": asdict(persona),
            "bundle_key": combo_key,
            "mode": mode,
            "provenance": self.provenance(mode, source_hash=combo_key),
            "mock_prefix": MOCK_PREFIX if mode == "mock" else "",
            "channels": [asdict(asset) for asset in assets],
        }
        self.write_output_json(combo_dir / "manifest.json", manifest)
        return manifest

    def write_health(self, report: dict[str, Any]) -> None:
        health_path = Path(self.config["operations"]["health_file_path"])
        health_payload = {**self.health_check(), **report}
        health_payload.setdefault("provenance", self.provenance(str(health_payload["mode"])))
        self.write_output_json(health_path, health_payload)

    def zip_bundle(self, bundle_root: Path) -> Path:
        zip_path = bundle_root.with_suffix(".zip")
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in sorted(bundle_root.rglob("*")):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(bundle_root))
        return zip_path

    def run(self) -> dict[str, Any]:
        with self.mutex():
            return self._run_locked()

    def _run_locked(self) -> dict[str, Any]:
        if self.stop_requested() and not self.override_enabled():
            report = {"status": "stopped", "generated_combo_count": 0, "error_count": 0}
            self.write_health(report)
            self.audit("run_stopped")
            return report
        self.preflight_validate()
        mode = self.resolve_mode()
        if mode == "real":
            self.verify_real_dispatch()
        failure_streak = 0
        threshold = int(self.config["lose_coupling"]["LC3_circuit_breaker"]["circuit_breaker_open_threshold"])
        bundle_root = self.bundle_root()
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for hotel in self.hotels:
            for persona in self.personas:
                if self.stop_requested() and not self.override_enabled():
                    break
                try:
                    results.append(self.generate_combo(hotel, persona, mode, bundle_root))
                    failure_streak = 0
                except Exception as exc:
                    failure_streak += 1
                    errors.append({"hotel_id": hotel.hotel_id, "persona_id": persona.persona_id, "error": str(exc)})
                    self.audit("combo_failed", hotel_id=hotel.hotel_id, persona_id=persona.persona_id, error=str(exc))
                    if failure_streak >= threshold:
                        self.audit("circuit_open", failure_streak=failure_streak)
                        break
            if failure_streak >= threshold:
                break
        report = {
            "status": "completed_with_errors" if errors else "completed",
            "mode": mode,
            "provenance": self.provenance(mode),
            "bundle_root": str(bundle_root),
            "generated_combo_count": len(results),
            "error_count": len(errors),
            "errors": errors,
            "channel_file_count": sum(len(entry["channels"]) for entry in results),
        }
        self.write_output_json(bundle_root / "run-manifest.json", report | {"combos": results})
        zip_path = self.zip_bundle(bundle_root)
        report["zip_path"] = str(zip_path)
        self.write_health(report)
        completion_event = "mock_run_complete" if mode == "mock" else "run_complete"
        self.audit(completion_event, **{k: v for k, v in report.items() if k != "errors"})
        return report


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    config_path = Path(argv[0]) if argv else Path(__file__).resolve().parents[1] / "config.yaml"
    pipeline = AssetPipeline(config_path)
    report = pipeline.run()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in {"completed", "completed_with_errors", "stopped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
