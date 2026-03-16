from enum import StrEnum


class VectorType(StrEnum):
    NOOP = "noop"
    MILVUS = "milvus"
    MARIADB_VECTOR = "mariadb-vector"
    QDRANT = "qdrant"
    CHROMA = "chroma"
    PINECONE = "pinecone"
    ELASTICSEARCH = "elasticsearch"
    OPENSEARCH = "opensearch"
    PGVECTOR = "pgvector"
    ORACLE23AI = "oracle23ai"
    S3VECTOR = "s3vector"
    WEAVIATE = "weaviate"
    OPENGAUSS = "opengauss"
