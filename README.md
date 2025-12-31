# Voyadecir Azure Functions — OCR Routing

This repository contains **Azure Functions** used by Voyadecir
for OCR routing and integration with Azure Document Intelligence.

It exists to support the backend API.
It does not implement product logic.

---

## What This Repo Owns

- Azure Functions for OCR execution
- Communication with Azure Document Intelligence Read OCR
- Polling and status handling
- Structured OCR responses back to the backend

---

## What This Repo Does NOT Own

- UI logic
- Translation
- Explanations
- Business rules
- Monetization logic
- Any direct client interaction

All product intelligence lives in the backend (`ai-translator`).

---

## OCR Behavior (Authoritative Pointer)

OCR behavior is **defined centrally** in the meta repo:

voyadecir-meta/OCR_DEBUG.md

This repository must:
- Use Azure Document Intelligence Read as primary
- Respect retry and timeout rules
- Return structured stage metadata
- Never return generic failures

If there is any conflict, the meta repo wins.

---

## Environment Variables (Required)

Azure OCR:
- AZURE_DI_ENDPOINT
- AZURE_DI_API_KEY
- AZURE_DI_API_VERSION
- AZURE_DI_MODEL=prebuilt-read

App Settings:
- OCR_CONFIDENCE_THRESHOLD=0.75 (optional)
- DEBUG_OCR=false (optional)

Never hardcode secrets.

---

## Deployment

- Hosted as Azure Functions
- Deployed via GitHub Actions or Azure tooling
- Environment variables set in Azure Function App settings

---

## Source of Truth

Authoritative rules live in the meta repo:

- Architecture: voyadecir-meta/README.md
- AI rules: voyadecir-meta/AGENTS.md
- Priorities: voyadecir-meta/TASKS.md
- OCR behavior: voyadecir-meta/OCR_DEBUG.md

If this README conflicts with those, those win.
This is a new file 
