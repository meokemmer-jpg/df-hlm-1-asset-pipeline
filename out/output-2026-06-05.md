# df-hlm-1-asset-pipeline — Output [CRUX-MK]
*Autonom aktiviert 2026-06-05T14:09:25.039896+00:00 | ollama-local/qwen2.5:14b-instruct*

# Dark-Factory Dokumentation: df-hlm-1-asset-pipeline

## Mission
Die Mission der Dark-Factory `df-hlm-1-asset-pipeline` ist die Erstellung e
eines Asset-Pipelines für den Hotel-Lifecycle-Marketing (HLM) für 7 Berline
Berliner Hotels und 8 wiederverwendbaren Personas.

### Aktivierungsmode
Die Factory kann in verschiedenen Modi aktiviert werden:

* **Mock-Mode** (Standard): Generiert Assets ohne echte Ausgabe.
* **Internal-Real Mode**: Aktiviert die echte Ausgabe mit `DF_HLM_1_ENABLE_
`DF_HLM_1_ENABLE_REAL_OUTPUT=1`.
* **External-Real Mode**: Erfordert direkte Modusaufhebung via `DF_HLM_1_DI
`DF_HLM_1_DIRECT_MODE`.

### K11-K16 Härtung
Die Factory ist für einen hohen Schwierigkeitsgrad (K11-K16) gehärtet:

* **Cascade Isolation**: Hard
* **Failure Blast Radius**: 1
* **Dependency DLQ Strategy**: Per-Combo-Isolierung mit Manifest-Fehlern un
und Audit-Logging
* **Provenance Required in Output**: True
* **Non-LLM Validation Layer**: HTML-Lint + deterministische Provenanz + Ma
Manifest-Digest

### Real-Run-Aktivierung
Um einen echten Lauf zu aktivieren, muss `DF_HLM_1_ENABLE_REAL_OUTPUT=1` ge
gesetzt werden und die Phronesis-Anforderungen erfüllt sein. Zusätzlich kan
kann `DF_HLM_1_DIRECT_MODE` verwendet werden, um den direkten Modus zu akti
aktivieren.

### Datenklassen
Die Factory verwendet mehrere Dataclasses zur Spezifikation der Pipeline:

| **Modul** | **Dataclass** | **Feld** | **Typ** | **Standardwert** |
| --- | --- | --- | --- | --- |
| `src/heylou_reisen_pipeline.py` | `TravelQuery` | `query_id` | str |  |
| `src/heylou_reisen_pipeline.py` | `TravelQuery` | `query_type` | TravelQu
TravelQueryType |  |
| `src/heylou_reisen_pipeline.py` | `TravelQuery` | `location_preference` |
| str \| None |  |
| `src/heylou_reisen_pipeline.py` | `TravelQuery` | `duration_nights` | int
int |  |

### Umgebungvariablen
Die Factory verwendet die folgenden Umgebungsvariablen:

* **DF_HLM_1_ENABLE_REAL_OUTPUT**: Aktiviert echte Ausgabe.
* **DF_HLM_1_DIRECT_MODE**: Aktiviert den direkten Modus.

Diese Dark-Factory dient zur Erstellung spezifischer Marketingassets für Be
Berliner Hotels basierend auf wiederverwendbaren Personas, um die Effektivi
Effektivität des Hotel-Lifecycle-Marketing zu steigern.