#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "evaluation/scripts/append_lifetime_fastpath_backup_slide.py"
DECK = REPOSITORY_ROOT / "docs/UniAlloc-Qualifier-Core-Deck.pptx"
IMAGE = (
    REPOSITORY_ROOT
    / "docs/figures/lifetime-resident-index-20260715"
    / "mixed-filler-fastpath-evidence-slide.png"
)
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
DOCUMENT_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MANAGED_REL_ID = "RlifetimeFastpathB13"

ElementTree.register_namespace("p", PRESENTATION_NS)
ElementTree.register_namespace("a", DRAWING_NS)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_package(source: Path, destination: Path, transform) -> None:
    with ZipFile(source) as package:
        infos = package.infolist()
        entries = {info.filename: package.read(info.filename) for info in infos}
    transform(entries)
    with ZipFile(destination, "w") as package:
        for info in infos:
            if info.filename in entries:
                package.writestr(info, entries.pop(info.filename))
        for name, data in entries.items():
            package.writestr(name, data)


def strip_managed_slide(source: Path, destination: Path) -> None:
    def transform(entries: dict[str, bytes]) -> None:
        for name in (
            "ppt/slides/slide32.xml",
            "ppt/slides/_rels/slide32.xml.rels",
            "ppt/media/lifetime-mixed-fastpath-b13.png",
        ):
            entries.pop(name, None)

        content_types = ElementTree.fromstring(entries["[Content_Types].xml"])
        for child in list(content_types):
            if child.tag == f"{{{CONTENT_TYPES_NS}}}Override" and child.get(
                "PartName"
            ) == "/ppt/slides/slide32.xml":
                content_types.remove(child)
            if child.tag == f"{{{CONTENT_TYPES_NS}}}Default" and child.get(
                "Extension", ""
            ).lower() == "png":
                content_types.remove(child)
        entries["[Content_Types].xml"] = ElementTree.tostring(
            content_types, encoding="utf-8", xml_declaration=True
        )

        relationships = ElementTree.fromstring(
            entries["ppt/_rels/presentation.xml.rels"]
        )
        for child in list(relationships):
            if child.get("Id") == MANAGED_REL_ID:
                relationships.remove(child)
        entries["ppt/_rels/presentation.xml.rels"] = ElementTree.tostring(
            relationships, encoding="utf-8", xml_declaration=True
        )

        presentation = ElementTree.fromstring(entries["ppt/presentation.xml"])
        relationship_attribute = f"{{{DOCUMENT_REL_NS}}}id"
        slide_list = presentation.find(f"{{{PRESENTATION_NS}}}sldIdLst")
        assert slide_list is not None
        for child in list(slide_list):
            if child.get(relationship_attribute) == MANAGED_REL_ID:
                slide_list.remove(child)
        entries["ppt/presentation.xml"] = ElementTree.tostring(
            presentation, encoding="utf-8", xml_declaration=True
        )

        for note_name in (
            "ppt/notesSlides/notesSlide20.xml",
            "ppt/notesSlides/notesSlide25.xml",
        ):
            note = ElementTree.fromstring(entries[note_name])
            for text_body in note.findall(f".//{{{PRESENTATION_NS}}}txBody"):
                for paragraph in list(text_body):
                    texts = paragraph.findall(f".//{{{DRAWING_NS}}}t")
                    if any((node.text or "").startswith("Backup jump: B13") for node in texts):
                        text_body.remove(paragraph)
            entries[note_name] = ElementTree.tostring(
                note, encoding="utf-8", xml_declaration=True
            )

    write_package(source, destination, transform)


def add_wrong_managed_target(source: Path, destination: Path) -> None:
    def transform(entries: dict[str, bytes]) -> None:
        relationships = ElementTree.fromstring(
            entries["ppt/_rels/presentation.xml.rels"]
        )
        ElementTree.SubElement(
            relationships,
            f"{{{PACKAGE_REL_NS}}}Relationship",
            {
                "Id": MANAGED_REL_ID,
                "Type": f"{DOCUMENT_REL_NS}/slide",
                "Target": "/ppt/slides/slide31.xml",
            },
        )
        entries["ppt/_rels/presentation.xml.rels"] = ElementTree.tostring(
            relationships, encoding="utf-8", xml_declaration=True
        )

        presentation = ElementTree.fromstring(entries["ppt/presentation.xml"])
        slide_list = presentation.find(f"{{{PRESENTATION_NS}}}sldIdLst")
        assert slide_list is not None
        ElementTree.SubElement(
            slide_list,
            f"{{{PRESENTATION_NS}}}sldId",
            {"id": "287", f"{{{DOCUMENT_REL_NS}}}id": MANAGED_REL_ID},
        )
        entries["ppt/presentation.xml"] = ElementTree.tostring(
            presentation, encoding="utf-8", xml_declaration=True
        )

    write_package(source, destination, transform)


class LifetimeFastpathDeckSlideTests(unittest.TestCase):
    def test_initial_insert_is_complete_and_second_run_is_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            deck = directory_path / "deck.pptx"
            manifest = directory_path / "manifest.json"
            strip_managed_slide(DECK, deck)
            with ZipFile(deck) as package:
                slides = [
                    name
                    for name in package.namelist()
                    if name.startswith("ppt/slides/slide")
                    and name.endswith(".xml")
                ]
                self.assertEqual(31, len(slides))

            command = [
                "python3",
                str(SCRIPT),
                "--deck",
                str(deck),
                "--manifest",
                str(manifest),
            ]
            subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            first_hash = sha256_file(deck)
            subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first_hash, sha256_file(deck))

            with ZipFile(deck) as package:
                names = package.namelist()
                slides = [
                    name
                    for name in names
                    if name.startswith("ppt/slides/slide")
                    and name.endswith(".xml")
                ]
                self.assertEqual(32, len(slides))
                self.assertEqual(1, names.count("ppt/slides/slide32.xml"))
                self.assertIn(
                    b"B13 Lifetime Mixed Fastpath Evidence",
                    package.read("ppt/slides/slide32.xml"),
                )
                embedded = package.read("ppt/media/lifetime-mixed-fastpath-b13.png")
                self.assertEqual(sha256_file(IMAGE), sha256_bytes(embedded))

                relationships = ElementTree.fromstring(
                    package.read("ppt/_rels/presentation.xml.rels")
                ).findall(f"{{{PACKAGE_REL_NS}}}Relationship")
                managed_relationships = [
                    row for row in relationships if row.get("Id") == MANAGED_REL_ID
                ]
                self.assertEqual(1, len(managed_relationships))
                self.assertEqual(
                    f"{DOCUMENT_REL_NS}/slide", managed_relationships[0].get("Type")
                )
                self.assertEqual(
                    "/ppt/slides/slide32.xml", managed_relationships[0].get("Target")
                )

                presentation = ElementTree.fromstring(
                    package.read("ppt/presentation.xml")
                )
                slide_ids = presentation.findall(f".//{{{PRESENTATION_NS}}}sldId")
                numeric_ids = [row.get("id") for row in slide_ids]
                self.assertEqual(len(numeric_ids), len(set(numeric_ids)))
                self.assertEqual(1, numeric_ids.count("287"))

                content_types = ElementTree.fromstring(
                    package.read("[Content_Types].xml")
                )
                overrides = content_types.findall(f"{{{CONTENT_TYPES_NS}}}Override")
                self.assertEqual(
                    1,
                    sum(
                        row.get("PartName") == "/ppt/slides/slide32.xml"
                        for row in overrides
                    ),
                )
                for note_number in (20, 25):
                    self.assertEqual(
                        1,
                        package.read(
                            f"ppt/notesSlides/notesSlide{note_number}.xml"
                        ).count(b"Backup jump: B13"),
                    )

            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(first_hash, manifest_data["deck_embedding"]["deck_sha256"])
            self.assertEqual(
                sha256_file(IMAGE), manifest_data["assets"]["png"]["sha256"]
            )

    def test_wrong_managed_relationship_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            pristine = directory_path / "pristine.pptx"
            collided = directory_path / "collided.pptx"
            strip_managed_slide(DECK, pristine)
            add_wrong_managed_target(pristine, collided)
            completed = subprocess.run(
                ["python3", str(SCRIPT), "--deck", str(collided)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("wrong target", completed.stderr)


if __name__ == "__main__":
    unittest.main()
