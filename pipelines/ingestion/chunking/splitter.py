# pipelines/ingestion/chunking/splitter.py
try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except ImportError:  # the pinned production image (langchain==0.1.5) still has this in-package
    from langchain.text_splitter import RecursiveCharacterTextSplitter

def split_text(text: str, chunk_size: int = 512, overlap: int = 50):
    """
    Splits text into overlapping chunks.
    Standard optimization for Embedding Models (most have 512 or 8192 limits).
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", ".", " ", ""]
    )
    
    chunks = splitter.create_documents([text])
    
    return [
        {
            "text": chunk.page_content,
            "metadata": {
                "chunk_index": i
            }
        }
        for i, chunk in enumerate(chunks)
    ]