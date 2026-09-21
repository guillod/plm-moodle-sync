"""Extract inclusive, one-based page ranges without changing cached PDFs."""

from io import BytesIO
import logging
import re
from threading import get_ident


class PDFError(ValueError):
    """A PDF selection could not be prepared for publication."""


def _append_pages(writer, reader, first, last):
    # pypdf 6.18 checks links while append temporarily excludes /Annots, then
    # rebuilds the selected links. Suppress only that intermediate size warning
    # on this thread; all other parser warnings remain available.
    logger = logging.getLogger('pypdf.generic._link')
    thread = get_ident()

    def keep_warning(record):
        return record.thread != thread or not re.fullmatch(
            r'Annotation sizes differ: [1-9][0-9]* vs\. 0', record.getMessage())

    logger.addFilter(keep_warning)
    try:
        writer.append(reader, pages=(first - 1, last))
    finally:
        logger.removeFilter(keep_warning)


def page_bounds(value):
    """Parse a page or ascending range, such as '3' or '1-11'."""
    match = re.fullmatch(r'([1-9][0-9]*)(?:-([1-9][0-9]*))?', value.strip()) if isinstance(value, str) else None
    if match:
        first, last = int(match[1]), int(match[2] or match[1])
        if first <= last:
            return first, last
    raise PDFError('Pages must be a quoted positive page number or ascending range, such as "1-11".')


def extract_pages(content, pages):
    """Return the selected PDF, or the original bytes when pages is omitted."""
    if pages is None:
        return content
    first, last = page_bounds(pages)
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.errors import PyPdfError
    except ImportError as error:
        raise PDFError('PDF page extraction requires pypdf in the active Python environment. '
                       'From the project directory, install with: python -m pip install -e .') from error
    try:
        with BytesIO(content) as source, BytesIO() as output:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise PDFError('Cannot extract pages from an encrypted PDF.')
            count = len(reader.pages)
            if last > count:
                raise PDFError(f'Requested page {last}, but the source PDF contains only {count} pages.')
            with PdfWriter() as writer:
                # append remaps links and imports outlines for the selected pages.
                _append_pages(writer, reader, first, last)
                writer.write(output)
            return output.getvalue()
    except PDFError:
        raise
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError, RecursionError) as error:
        # PDF parser errors may contain document data. Report only a safe summary.
        raise PDFError('Could not extract pages from the source PDF; check that it is a valid PDF.') from error
