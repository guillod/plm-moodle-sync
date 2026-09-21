"""Page selection with real PDF content, independent page labels, and outlines."""

from io import BytesIO
import sys
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from plm_moodle_sync.common.pdf import PDFError, extract_pages


def make_pdf(labels):
    with PdfWriter() as writer, BytesIO() as output:
        for index, label in enumerate(labels):
            page = writer.add_blank_page(width=300 + index, height=400)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({
                NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 20 200 Td ({label}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
            writer.add_outline_item('Page ' + str(index + 1), index)
        writer.write(output)
        return output.getvalue()


class PageExtractionTests(unittest.TestCase):
    def test_inclusive_first_eleven_pages_keep_text_page_boxes_and_outlines(self):
        original = make_pdf(['Page ' + str(number) for number in range(1, 14)])
        result = extract_pages(original, '1-11')
        reader = PdfReader(BytesIO(result))
        self.assertEqual(len(reader.pages), 11)
        self.assertEqual([page.extract_text() for page in reader.pages],
                         ['Page ' + str(number) for number in range(1, 12)])
        self.assertEqual([page.mediabox.width for page in reader.pages], list(range(300, 311)))
        self.assertEqual([reader.get_destination_page_number(item) for item in reader.outline], list(range(11)))
        self.assertEqual(len(PdfReader(BytesIO(original)).pages), 13)

    def test_middle_range_and_single_page_use_physical_page_positions(self):
        original = make_pdf(['Cover', 'Contents', 'Chapter one', 'Chapter two', 'Appendix'])
        for selection, expected in [('3-4', ['Chapter one', 'Chapter two']), ('5', ['Appendix'])]:
            with self.subTest(selection=selection):
                reader = PdfReader(BytesIO(extract_pages(original, selection)))
                self.assertEqual([page.extract_text() for page in reader.pages], expected)

    def test_repeated_extraction_has_identical_bytes(self):
        original = make_pdf(['one', 'two', 'three'])
        self.assertEqual(extract_pages(original, '1-2'), extract_pages(original, '1-2'))

    def test_keeps_internal_and_external_links_and_removes_links_to_excluded_pages(self):
        with PdfWriter() as writer, BytesIO() as output:
            writer.clone_document_from_reader(PdfReader(BytesIO(make_pdf(['one', 'two', 'three']))))
            writer.add_annotation(0, Link(rect=(0, 0, 10, 10), target_page_index=1))
            writer.add_annotation(0, Link(rect=(0, 20, 10, 30), target_page_index=2))
            writer.add_annotation(0, Link(rect=(0, 40, 10, 50), url='https://example.org'))
            writer.write(output)
            original = output.getvalue()
        with self.assertNoLogs('pypdf.generic._link', level='WARNING'):
            selected = PdfReader(BytesIO(extract_pages(original, '1-2')))
        links = [link.get_object() for link in selected.pages[0]['/Annots']]
        self.assertEqual(len(links), 2)
        internal = next(link for link in links if '/Dest' in link)
        self.assertEqual(selected.get_page_number(internal['/Dest'][0].get_object()), 1)
        external = next(link for link in links if '/A' in link)
        self.assertEqual(external['/A']['/URI'], 'https://example.org')

    def test_changes_outside_selection_do_not_change_extracted_bytes(self):
        self.assertEqual(extract_pages(make_pdf(['one', 'two', 'three']), '1-2'),
                         extract_pages(make_pdf(['one', 'two', 'changed']), '1-2'))

    def test_omitted_pages_preserve_original_bytes_without_loading_pypdf(self):
        original = b'%PDF-original-data'
        with patch.dict(sys.modules, {'pypdf': None}):
            self.assertIs(extract_pages(original, None), original)
            with self.assertRaisesRegex(PDFError, 'requires pypdf'):
                extract_pages(original, '1-2')

    def test_out_of_bounds_is_rejected_instead_of_silently_truncating(self):
        with self.assertRaisesRegex(PDFError, 'page 11.*only 3 pages'):
            extract_pages(make_pdf(['one', 'two', 'three']), '1-11')

    def test_invalid_pdf_has_a_safe_error(self):
        for content in (b'private-secret', b'%PDF-1.7\nprivate-secret\n%%EOF'):
            with self.subTest(content=content), self.assertRaisesRegex(PDFError, 'valid PDF') as caught:
                extract_pages(content, '1')
            self.assertNotIn('private-secret', str(caught.exception))

    def test_encrypted_pdf_is_rejected(self):
        with PdfWriter() as writer, BytesIO() as output:
            writer.add_blank_page(width=300, height=400)
            writer.encrypt('test-password', algorithm='RC4-128')
            writer.write(output)
            encrypted = output.getvalue()
        with self.assertRaisesRegex(PDFError, 'encrypted PDF'):
            extract_pages(encrypted, '1')
