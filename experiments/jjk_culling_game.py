#!/usr/bin/env python3
"""Zero-key, isolated JJK explainer fixture for OpenMontage's existing Explainer."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "remotion-composer" / "public"
PUBLIC.mkdir(parents=True, exist_ok=True)
scenes = [
    ("THE CULLING GAME", "Kenjaku's deadly ritual", "hero_title"),
    ("10 COLONIES", "Japan becomes a battlefield", "stat_card"),
    ("RULE 1", "Players enter deadly colonies", "text_card"),
    ("5 POINTS", "Defeat a sorcerer", "stat_card"),
    ("1 POINT", "Defeat a non-sorcerer", "stat_card"),
    ("100 POINTS", "Propose a new rule", "stat_card"),
    ("GAME MASTER", "Can reject rules that disrupt the game", "text_card"),
    ("19 DAYS", "No point change means cursed technique removal", "stat_card"),
    ("NO EASY EXIT", "Entering means playing by the rules", "hero_title"),
    ("THE REAL PURPOSE", "Generate cursed energy", "hero_title"),
    ("KENJAKU'S PLAN", "A ritual involving Tengen", "hero_title"),
    ("NOT ABOUT WINNING", "The game is the ritual", "hero_title"),
]
cuts = []
for i, (headline, detail, kind) in enumerate(scenes):
    start = i * 5.25
    cut = {"id": f"jjk-{i:02}", "source": "remotion", "type": kind,
           "in_seconds": start, "out_seconds": start + 5.25,
           "accentColor": "#e63946", "color": "#fff1ee"}
    if kind == "stat_card":
        cut.update(stat=headline, subtitle=detail)
    elif kind == "hero_title":
        cut.update(text=headline, heroSubtitle=detail)
    else:
        cut.update(text=f"{headline}\n{detail}")
    cuts.append(cut)
props = {
    "theme": "flat-motion-graphics",
    "cuts": cuts,
    "overlays": [],
    "captions": [],
    "audio": {"narration": {"src": "jjk-test/narration.wav", "volume": 1}},
}
dest = PUBLIC / "jjk-test" / "props.json"
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text(json.dumps(props, indent=2), encoding="utf-8")
print(f"Prepared {len(cuts)} scenes, 64-second composition: {dest}")
