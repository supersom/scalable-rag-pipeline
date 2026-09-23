# pipelines/ingestion/indexing/neo4j.py
from neo4j import GraphDatabase
from typing import Dict, Any, List
import os

class Neo4jIndexer:
    """
    Writes data to Neo4j Graph Database.
    Implements efficient Batch Writes.
    """
    # Class-level, not instance-level: a fresh Neo4jIndexer() is constructed per
    # map_batches call, but the fulltext index only needs creating once per worker
    # process, not once per batch — ensure_neo4j_fulltext_index() in scripts/ingest_local.py
    # does the equivalent once at startup; this mirrors that for the Ray path, which had no
    # such call at all, so retrieve_node's entity_index fulltext query would fail on data
    # written through this path alone.
    _index_ensured = False

    def __init__(self):
        # Read from Env Vars (injected by Ray Cluster environment)
        uri = os.getenv("NEO4J_URI", "bolt://neo4j-cluster:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "changeme")

        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self._ensure_fulltext_index()

    def _ensure_fulltext_index(self):
        cls = type(self)
        if cls._index_ensured:
            return
        with self.driver.session() as session:
            session.run("CREATE FULLTEXT INDEX entity_index IF NOT EXISTS FOR (n:Entity) ON EACH [n.name]")
        cls._index_ensured = True

    def write(self, batch: List[Dict[str, Any]]):
        """
        Called by Ray Data `write_datasource`.
        Receives a list of rows (dicts).
        """
        # 1. Flatten the batch into lists of all nodes and edges
        all_nodes = []
        all_edges = []
        
        for row in batch:
            # Each row corresponds to a text chunk
            if "graph_nodes" in row:
                all_nodes.extend(row["graph_nodes"])
            if "graph_edges" in row:
                all_edges.extend(row["graph_edges"])
        
        if not all_nodes and not all_edges:
            return

        # 2. Execute Batch Write Transaction
        with self.driver.session() as session:
            session.execute_write(self._merge_graph_data, all_nodes, all_edges)

    @staticmethod
    def _merge_graph_data(tx, nodes, edges):
        """
        Cypher query to idempotent MERGE (Upsert) data.
        """
        # 1. Merge Nodes
        # UNWIND expands the list $nodes into individual rows
        cypher_nodes = """
        UNWIND $nodes AS n
        MERGE (node:Entity {name: n.id})
        SET node.type = n.type
        """
        tx.run(cypher_nodes, nodes=nodes)
        
        # 2. Merge Edges
        # Matches existing source/target and creates relationship if missing
        cypher_edges = """
        UNWIND $edges AS e
        MATCH (source:Entity {name: e.source})
        MATCH (target:Entity {name: e.target})
        MERGE (source)-[r:RELATED {type: e.type}]->(target)
        """
        tx.run(cypher_edges, edges=edges)