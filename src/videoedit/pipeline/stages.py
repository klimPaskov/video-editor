from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    PROJECT_INIT = "project_init"
    INGEST = "ingest"
    TRANSCRIBE = "transcribe"
    SILENCE_ANALYSIS = "silence_analysis"
    EDIT_PROPOSAL = "edit_proposal"
    EDIT_APPROVAL = "edit_approval"
    ROUGH_RENDER = "rough_render"
    AUDIO_FINISH = "audio_finish"
    CAPTIONS = "captions"
    ASSET_PLANNING = "asset_planning"
    ASSET_GENERATION = "asset_generation"
    PREVIEW_RENDER = "preview_render"
    QA = "qa"
    FINAL_RENDER = "final_render"
    DELIVERY = "delivery"
