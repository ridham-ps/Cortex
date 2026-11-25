"""How a repo becomes searchable (no LLM).

    ingest.py        clone/read a repo, chunk it, write the index (entry: ingest_repo)
    chunker.py       split files into chunks at function/class/heading boundaries
    enrich.py        add i18n labels and recent git commits as searchable text
    store.py         the search index (TF-IDF by default) + get_store()
    dense_store.py   embedding search (optional)
    hybrid_store.py  TF-IDF + embeddings combined (optional)
    access.py        ONBOARDING_ALLOWED_ROOTS: which folders may be indexed
"""
