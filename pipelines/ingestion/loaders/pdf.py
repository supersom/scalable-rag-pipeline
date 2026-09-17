# pipelines/ingestion/loaders/pdf.py
import io
import logging
import os
import signal
import tempfile
from typing import Tuple, Dict, Any, List

from pypdf import PdfReader, PdfWriter
from unstructured.partition.pdf import partition_pdf

logger = logging.getLogger(__name__)

# A single malformed page (corrupt content-stream operators, seen live on
# real files) can make pdfminer's interpreter loop for minutes to hours.
# partition_pdf() is one atomic call over however many pages you give it, so
# calling it on the whole document means one bad page costs the whole file —
# including every page that would otherwise have parsed fine. Splitting into
# small page-batches, each independently timed and skippable, bounds the
# damage to one batch: a 500-page book with one broken page still gets ~490
# pages indexed instead of zero.
PAGES_PER_BATCH = int(os.getenv("INGESTION_PDF_PAGES_PER_BATCH", "10"))
BATCH_TIMEOUT_SECONDS = int(os.getenv("INGESTION_PDF_BATCH_TIMEOUT_SECONDS", "30"))


class _PageBatchTimeout(BaseException):
    """Deliberately not Exception — see pipelines/ingestion/main.py's
    _ParseTimeout for why: unstructured/pdfminer wrap individual parsing
    steps in their own broad except Exception:, which would otherwise
    swallow this silently and leave the rest of the batch unprotected."""


def _raise_batch_timeout(signum, frame):
    raise _PageBatchTimeout()


def _parse_page_batch(page_bytes: bytes, batch_label: str) -> Tuple[str, List[str]]:
    """
    Runs partition_pdf on one small page-batch's own byte stream, under a
    short timeout. A hang or malformed content stream here only costs this
    batch, not the rest of the document.
    """
    text = ""
    tables: List[str] = []

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp_file:
        tmp_file.write(page_bytes)
        tmp_file.flush()

        previous_handler = signal.signal(signal.SIGALRM, _raise_batch_timeout)
        signal.alarm(BATCH_TIMEOUT_SECONDS)
        try:
            elements = partition_pdf(
                filename=tmp_file.name,
                strategy="fast",
                include_page_breaks=True,
                infer_table_structure=True,
            )
            for el in elements:
                text += str(el) + "\n"
                if el.category == "Table" and hasattr(el.metadata, "text_as_html"):
                    tables.append(el.metadata.text_as_html)
        except _PageBatchTimeout:
            logger.error(
                f"Timed out parsing {batch_label} after {BATCH_TIMEOUT_SECONDS}s, "
                f"skipping batch"
            )
        except Exception as e:
            logger.error(f"Failed to parse {batch_label}: {e}")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)

    return text, tables


def parse_pdf_bytes(file_bytes: bytes, filename: str) -> Tuple[str, Dict[str, Any]]:
    """
    Parses a PDF in small page-batches rather than as one whole-document
    call to partition_pdf, so a single malformed or pathologically slow page
    only costs its own batch's timeout budget instead of losing the entire
    file's content.

    Args:
        file_bytes: The raw bytes of the PDF file.
        filename: Original filename for metadata.

    Returns:
        Tuple containing (extracted_text_content, metadata_dict)
    """
    reader = PdfReader(io.BytesIO(file_bytes))
    num_pages = len(reader.pages)

    text_content = ""
    tables: List[str] = []

    for start in range(0, num_pages, PAGES_PER_BATCH):
        end = min(start + PAGES_PER_BATCH, num_pages)

        writer = PdfWriter()
        for page_index in range(start, end):
            writer.add_page(reader.pages[page_index])

        batch_buffer = io.BytesIO()
        writer.write(batch_buffer)

        batch_label = f"{filename} pages {start + 1}-{end}"
        batch_text, batch_tables = _parse_page_batch(
            batch_buffer.getvalue(), batch_label
        )
        text_content += batch_text
        tables.extend(batch_tables)

    metadata = {
        "filename": filename,
        "type": "pdf",
        "has_tables": len(tables) > 0,
        "table_count": len(tables),
        "page_count": num_pages,
    }

    return text_content, metadata
