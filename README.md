# ARTIFEX 🖨️

**ARTIFEX** is a fully custom-built CoreXY 3D printer with an integrated AI pipeline — type a description, and ARTIFEX generates and prints a 3D model. No CAD skills required.

> Built by Malte, Leo and Bo · Jugend Forscht 2027 · First prototype planned: February 2027

---

## What is ARTIFEX?

Most 3D printers require you to either download a ready-made model or design one yourself in CAD software. ARTIFEX removes that barrier entirely. Its AI pipeline takes a text prompt, automatically decides how to best generate a printable 3D model, and sends it straight to the printer via a custom slicer.

> 🚧 **Early development** — 3D model generation pipeline is just getting started. First full prototype targeted for February 2027.

---

## Features

### 🤖 AI Model Generation Pipeline

The pipeline starts with a prompt. An AI model (via OpenAI API — running locally or in the cloud) refines the prompt and decides whether the object is best represented as **organic** or **parametric**:

- **Organic path:** [z-image-turbo](https://huggingface.co/) generates an image → [Hunyuan3D-2.1](https://github.com/Tencent/Hunyuan3D-2) converts it to a 3D mesh
- **Parametric path:** AI generates OpenSCAD code directly for precise, dimension-accurate objects

Both paths feed into a **local custom slicer** with AI-assisted print settings.

```
              Prompt
                │
                ▼
    AI Model (prompt refinement)
    decides: Organic or Parametric?
    (via OpenAI API — local or cloud)
               / \
              /   \
             ▼     ▼
     Z-Image Turbo   AI generates
    (generates image)   OpenSCAD
             │               │
             ▼               │
       Hunyuan3D-2.1         │
             │               │
             └──────┬────────┘
                    ▼
        Local Slicing (custom slicer)
          with AI-assisted settings
                    │
                    ▼
                ARTIFEX 🖨️
```

### 🔩 Custom CoreXY Hardware
- **Frame:** 4040 aluminum extrusion — stiff, fast, scalable
- **Bed:** Ender 3 heated bed (235 × 235 mm), Z-axis only (no bed slinger)
- **Mainboard:** BTT SKR Pico Mini
- **Hotend:** E3D V6
- **Extruder:** Direct Drive
- **Motors:** NEMA17
- **Firmware:** Klipper

### 🧲 Modular Magnetic Toolhead
The toolhead uses a magnetic quick-swap system — different tools can be attached and detached in seconds without screws or manual wiring.

### 🎨 ARTIFEX Filament System (AFS)
ARTIFEX runs its own **magnetic modular filament system** — a fully custom alternative to proprietary solutions like Bambu's AMS. The AFS handles multi-material and color changes mid-print and is designed to be open, modular, and extendable. Modules connect magnetically and can be rearranged or expanded freely.

### 🌬️ HEPA + ACF Filtration
Integrated dual-stage filtration captures ultrafine particles and VOCs — important for enclosed printing with engineering filaments.

---

## Hardware Overview

| Component | Part |
|---|---|
| Frame | 4040 aluminum extrusion |
| Kinematic system | CoreXY |
| Bed | Ender 3 (235 × 235 mm, heated) |
| Mainboard | BTT SKR Pico Mini |
| Hotend | E3D V6 |
| Extruder | Direct Drive |
| Motors | NEMA17 |
| Firmware | Klipper |
| Filtration | HEPA + ACF |
| Toolhead mount | Magnetic quick-swap |
| Multi-material | ARTIFEX Filament System (AFS) |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Prompt refinement & routing | AI model via OpenAI API (local or cloud) |
| Image generation | z-image-turbo |
| 3D mesh generation | Hunyuan3D-2.1 |
| Parametric models | OpenSCAD + AI codegen |
| Slicing | Custom slicer (in-house) and Orca Compatibility |
| Printer firmware | Klipper / Moonraker |

---

## Roadmap

- [ ] Hardware design & component selection (in progress)
- [ ] CoreXY frame assembly
- [ ] Klipper firmware setup
- [ ] AFS (ARTIFEX Filament System) — magnetic modular design
- [ ] AI pipeline — prompt routing (organic vs. parametric)
- [ ] z-image-turbo + Hunyuan3D-2.1 integration
- [ ] OpenSCAD code generation integration
- [ ] Custom slicer development
- [ ] **First full prototype — February 2027** 🎯

---

## Team

Built by Malte, Leo and Bo for **Jugend Forscht 2027**.
