#!/usr/bin/env python3
"""Append or refresh the source-bound B13 image slide in the qualifier deck."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import tempfile
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DECK = REPOSITORY_ROOT / "docs/UniAlloc-Qualifier-Core-Deck.pptx"
DEFAULT_IMAGE = (
    REPOSITORY_ROOT
    / "docs/figures/lifetime-resident-index-20260715"
    / "mixed-filler-fastpath-evidence-slide.png"
)
DEFAULT_SVG = DEFAULT_IMAGE.with_suffix(".svg")
DEFAULT_MANIFEST = DEFAULT_IMAGE.parent / "slide-data-manifest.json"
DEFAULT_SUMMARY = (
    REPOSITORY_ROOT
    / "docs/evidence/lifetime-resident-index-20260715"
    / "mixed-filler-fastpath-swc-quick-summary.json"
)

SLIDE_NAME = "ppt/slides/slide32.xml"
SLIDE_RELS_NAME = "ppt/slides/_rels/slide32.xml.rels"
IMAGE_NAME = "ppt/media/lifetime-mixed-fastpath-b13.png"
PRESENTATION_REL_ID = "RlifetimeFastpathB13"
IMAGE_REL_ID = "RlifetimeFastpathImage"
MARKER = "B13 Lifetime Mixed Fastpath Evidence"
FIXED_ZIP_TIME = (2026, 7, 15, 12, 0, 0)
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
DOCUMENT_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
SLIDE_REL_TYPE = f"{DOCUMENT_REL_NS}/slide"
SLIDE_TARGET = "/ppt/slides/slide32.xml"
SLIDE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
)
ElementTree.register_namespace("p", PRESENTATION_NS)
ElementTree.register_namespace("a", DRAWING_NS)
ElementTree.register_namespace("r", DOCUMENT_REL_NS)

NOTE_JUMPS = {
    "ppt/notesSlides/notesSlide20.xml": (
        "Backup jump: B13 - cache-first mixed filling, physical-backing gates, "
        "and peak RSS. Current diagnostic; keep the claim-grade boundary visible."
    ),
    "ppt/notesSlides/notesSlide25.xml": (
        "Backup jump: B13 - SWC N=8 packing, fastpath, time, RSS, and per-sample "
        "backing gates. No default or Google TCMalloc comparison."
    ),
}


SLIDE_XML = f'''<?xml version="1.0" encoding="utf-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" showMasterSp="0">
  <p:cSld name="{MARKER}">
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
      <p:pic>
        <p:nvPicPr>
          <p:cNvPr id="2" name="{MARKER}" descr="Source-bound SWC mixed-filler diagnostic; claim eligibility false"/>
          <p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr>
          <p:nvPr/>
        </p:nvPicPr>
        <p:blipFill><a:blip r:embed="{IMAGE_REL_ID}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
        <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="12192000" cy="6858000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
      </p:pic>
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>
'''.encode()

SLIDE_RELS_XML = f'''<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="/ppt/slideLayouts/slideLayout1.xml" Id="RlifetimeFastpathLayout"/>
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="/ppt/media/lifetime-mixed-fastpath-b13.png" Id="{IMAGE_REL_ID}"/>
</Relationships>
'''.encode()


def validate_content_types(content_types: bytes) -> tuple[bool, bool]:
    root = ElementTree.fromstring(content_types)
    defaults = root.findall(f"{{{CONTENT_TYPES_NS}}}Default")
    png_defaults = [
        row for row in defaults if row.get("Extension", "").lower() == "png"
    ]
    if len(png_defaults) > 1:
        raise ValueError("duplicate PNG content-type defaults")
    if png_defaults and png_defaults[0].get("ContentType") != "image/png":
        raise ValueError("PNG content-type default has the wrong MIME type")

    overrides = root.findall(f"{{{CONTENT_TYPES_NS}}}Override")
    slide_overrides = [
        row for row in overrides if row.get("PartName") == f"/{SLIDE_NAME}"
    ]
    if len(slide_overrides) > 1:
        raise ValueError("duplicate slide 32 content-type overrides")
    if slide_overrides and slide_overrides[0].get("ContentType") != SLIDE_CONTENT_TYPE:
        raise ValueError("slide 32 content-type override has the wrong MIME type")
    return bool(png_defaults), bool(slide_overrides)


def validate_presentation_links(entries: dict[str, bytes]) -> bool:
    relationships = ElementTree.fromstring(
        entries["ppt/_rels/presentation.xml.rels"]
    ).findall(f"{{{PACKAGE_REL_NS}}}Relationship")
    relationship_ids = [row.get("Id") for row in relationships]
    if len(relationship_ids) != len(set(relationship_ids)):
        raise ValueError("presentation relationship IDs are not unique")
    managed_relationships = [
        row for row in relationships if row.get("Id") == PRESENTATION_REL_ID
    ]
    target_relationships = [
        row
        for row in relationships
        if row.get("Target") in {SLIDE_TARGET, SLIDE_TARGET.removeprefix("/")}
    ]

    presentation = ElementTree.fromstring(entries["ppt/presentation.xml"])
    relationship_attribute = f"{{{DOCUMENT_REL_NS}}}id"
    slide_ids = presentation.findall(f".//{{{PRESENTATION_NS}}}sldId")
    numeric_ids = [row.get("id") for row in slide_ids]
    if len(numeric_ids) != len(set(numeric_ids)):
        raise ValueError("numeric presentation slide IDs are not unique")
    managed_slide_ids = [
        row for row in slide_ids if row.get(relationship_attribute) == PRESENTATION_REL_ID
    ]
    reserved_numeric_ids = [row for row in slide_ids if row.get("id") == "287"]

    managed = bool(managed_relationships or managed_slide_ids)
    if bool(managed_relationships) != bool(managed_slide_ids):
        raise ValueError("managed slide relationship and slide ID are inconsistent")
    if not managed:
        if target_relationships:
            raise ValueError("slide 32 target is owned by an unexpected relationship ID")
        if reserved_numeric_ids:
            raise ValueError("numeric slide ID 287 is already reserved")
        return False

    if len(managed_relationships) != 1 or len(managed_slide_ids) != 1:
        raise ValueError("managed slide relationship or slide ID is duplicated")
    relationship = managed_relationships[0]
    if relationship.get("Type") != SLIDE_REL_TYPE:
        raise ValueError("managed presentation relationship has the wrong type")
    if relationship.get("Target") != SLIDE_TARGET:
        raise ValueError("managed presentation relationship has the wrong target")
    if target_relationships != managed_relationships:
        raise ValueError("multiple relationships target managed slide 32")
    if managed_slide_ids[0].get("id") != "287" or reserved_numeric_ids != managed_slide_ids:
        raise ValueError("managed slide must uniquely own numeric slide ID 287")
    return True


def add_or_validate_package_links(entries: dict[str, bytes]) -> None:
    content_types = entries["[Content_Types].xml"]
    has_png, has_slide_override = validate_content_types(content_types)
    content_types_root = ElementTree.fromstring(content_types)
    if not has_png:
        png_default = ElementTree.Element(
            f"{{{CONTENT_TYPES_NS}}}Default",
            {"Extension": "png", "ContentType": "image/png"},
        )
        first_override = next(
            (
                index
                for index, child in enumerate(content_types_root)
                if child.tag == f"{{{CONTENT_TYPES_NS}}}Override"
            ),
            len(content_types_root),
        )
        content_types_root.insert(first_override, png_default)
    if not has_slide_override:
        ElementTree.SubElement(
            content_types_root,
            f"{{{CONTENT_TYPES_NS}}}Override",
            {
                "PartName": f"/{SLIDE_NAME}",
                "ContentType": SLIDE_CONTENT_TYPE,
            },
        )
    content_types = ElementTree.tostring(
        content_types_root, encoding="utf-8", xml_declaration=True
    )
    entries["[Content_Types].xml"] = content_types
    validate_content_types(content_types)

    managed_links_exist = validate_presentation_links(entries)
    presentation_rels_root = ElementTree.fromstring(
        entries["ppt/_rels/presentation.xml.rels"]
    )
    if not managed_links_exist:
        ElementTree.SubElement(
            presentation_rels_root,
            f"{{{PACKAGE_REL_NS}}}Relationship",
            {
                "Type": SLIDE_REL_TYPE,
                "Target": SLIDE_TARGET,
                "Id": PRESENTATION_REL_ID,
            },
        )
    entries["ppt/_rels/presentation.xml.rels"] = ElementTree.tostring(
        presentation_rels_root, encoding="utf-8", xml_declaration=True
    )

    presentation_root = ElementTree.fromstring(entries["ppt/presentation.xml"])
    if not managed_links_exist:
        slide_list = presentation_root.find(f"{{{PRESENTATION_NS}}}sldIdLst")
        if slide_list is None:
            raise ValueError("presentation has no slide ID list")
        ElementTree.SubElement(
            slide_list,
            f"{{{PRESENTATION_NS}}}sldId",
            {"id": "287", f"{{{DOCUMENT_REL_NS}}}id": PRESENTATION_REL_ID},
        )
    entries["ppt/presentation.xml"] = ElementTree.tostring(
        presentation_root, encoding="utf-8", xml_declaration=True
    )
    validate_presentation_links(entries)


def add_note_jump(note_xml: bytes, text: str) -> bytes:
    root = ElementTree.fromstring(note_xml)
    namespaces = {"p": PRESENTATION_NS, "a": DRAWING_NS}
    for shape in root.findall(".//p:sp", namespaces):
        placeholder = shape.find("./p:nvSpPr/p:nvPr/p:ph", namespaces)
        if placeholder is None or placeholder.get("type") != "body":
            continue
        text_body = shape.find("./p:txBody", namespaces)
        if text_body is None:
            raise ValueError("notes body placeholder has no text body")
        if any(node.text == text for node in text_body.findall(".//a:t", namespaces)):
            return ElementTree.tostring(
                root, encoding="utf-8", xml_declaration=True
            )
        paragraph = ElementTree.SubElement(text_body, f"{{{DRAWING_NS}}}p")
        run = ElementTree.SubElement(paragraph, f"{{{DRAWING_NS}}}r")
        ElementTree.SubElement(run, f"{{{DRAWING_NS}}}t").text = text
        return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    raise ValueError("notes slide has no body placeholder")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(path.resolve())


def png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:29]
    if len(header) != 29 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("slide image is not a PNG")
    width, height = struct.unpack(">II", header[16:24])
    bit_depth, color_type = header[24], header[25]
    if bit_depth != 8 or color_type != 2:
        raise ValueError("slide PNG must be 8-bit RGB")
    return width, height


def svg_dimensions(path: Path) -> tuple[int, int]:
    root = ElementTree.parse(path).getroot()
    return int(root.get("width", "0")), int(root.get("height", "0"))


def write_manifest(
    manifest_path: Path, deck: Path, image: Path, svg: Path, summary_path: Path
) -> None:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    png_width, png_height = png_dimensions(image)
    svg_width, svg_height = svg_dimensions(svg)
    manifest = {
        "assets": {
            "png": {
                "height_px": png_height,
                "path": image.name,
                "sha256": sha256_file(image),
                "width_px": png_width,
            },
            "svg": {
                "height_px": svg_height,
                "path": svg.name,
                "sha256": sha256_file(svg),
                "width_px": svg_width,
            },
        },
        "claim_boundary": {
            "claim_grade": summary["claim_grade"],
            "dirty_working_tree_snapshot": True,
            "performance_claim_eligible": summary["performance_claim_eligible"],
            "presentation_claim_eligible": summary["presentation_claim_eligible"],
            "scope": "SWC fixed-work packing, fastpath, and physical-backing mechanism",
        },
        "deck_embedding": {
            "deck_path": display_path(deck),
            "deck_sha256": sha256_file(deck),
            "physical_slide_number": 32,
            "presentation_label": "B13",
        },
        "generators": {
            "deck_appender": {
                "path": display_path(Path(__file__)),
                "sha256": sha256_file(Path(__file__)),
            },
            "svg_renderer": {
                "path": display_path(
                    REPOSITORY_ROOT
                    / "evaluation/scripts/render_lifetime_mixed_fastpath_slide.py"
                ),
                "sha256": sha256_file(
                    REPOSITORY_ROOT
                    / "evaluation/scripts/render_lifetime_mixed_fastpath_slide.py"
                ),
            },
        },
        "raw_artifact": {
            "path_relative_to_source_worktree": summary["artifact"]["path"],
            "sha256": summary["artifact"]["sha256"],
        },
        "schema_version": 1,
        "source_summary": {
            "path": display_path(summary_path),
            "sha256": sha256_file(summary_path),
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def append_slide(deck: Path, image: Path, output: Path) -> None:
    with ZipFile(deck) as source:
        infos = source.infolist()
        entries = {info.filename: source.read(info.filename) for info in infos}

    numbered_slides = {
        int(match.group(1)): name
        for name in entries
        if (match := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name))
    }
    if any(number > 32 for number in numbered_slides):
        raise ValueError("deck already has slides after reserved B13 slide 32")
    if SLIDE_NAME in entries and MARKER.encode() not in entries[SLIDE_NAME]:
        raise ValueError("slide 32 exists and is not the managed B13 slide")
    managed_links_exist = validate_presentation_links(entries)
    if managed_links_exist != (SLIDE_NAME in entries):
        raise ValueError("managed slide part and presentation links are inconsistent")

    add_or_validate_package_links(entries)
    for note_name, note_text in NOTE_JUMPS.items():
        entries[note_name] = add_note_jump(entries[note_name], note_text)
    entries[SLIDE_NAME] = SLIDE_XML
    entries[SLIDE_RELS_NAME] = SLIDE_RELS_XML
    entries[IMAGE_NAME] = image.read_bytes()
    validate_presentation_links(entries)

    ordered_names = [info.filename for info in infos]
    for name in (SLIDE_NAME, SLIDE_RELS_NAME, IMAGE_NAME):
        if name not in ordered_names:
            ordered_names.append(name)
    original_infos = {info.filename: info for info in infos}

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        with ZipFile(temporary, "w") as destination:
            for name in ordered_names:
                info = original_infos.get(name)
                if info is None:
                    info = ZipInfo(name, FIXED_ZIP_TIME)
                    info.compress_type = ZIP_DEFLATED
                destination.writestr(info, entries[name])
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deck", type=Path, default=DEFAULT_DECK)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--svg", type=Path, default=DEFAULT_SVG)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output or args.deck
    append_slide(args.deck, args.image, output)
    manifest = args.manifest
    if (
        manifest is None
        and output.resolve() == DEFAULT_DECK.resolve()
        and args.image.resolve() == DEFAULT_IMAGE.resolve()
    ):
        manifest = DEFAULT_MANIFEST
    if manifest is not None:
        write_manifest(manifest, output, args.image, args.svg, args.summary)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
