import os
import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from dotenv import load_dotenv

load_dotenv()

class VectorStore:
    def __init__(self, db_path: str = None):
        if not db_path:
            db_path = os.getenv("CHROMA_DB_PATH", "./chroma_db")
        
        # Ensure directory exists
        os.makedirs(db_path, exist_ok=True)
        
        # Initialize chroma client
        self.client = chromadb.PersistentClient(path=db_path)
        
        # Setup OpenAI embedding function
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY must be set in the environment or .env file.")
            
        self.embedding_function = OpenAIEmbeddingFunction(
            api_key=api_key,
            model_name="text-embedding-3-small"
        )
        
        # Get or create collection
        self.collection = self.client.get_or_create_collection(
            name="employees",
            embedding_function=self.embedding_function
        )

    def add_employee(self, name: str, designation: str, phone_number: str, team: str, technology: str):
        """
        Adds an employee profile to the vector database.
        """
        # Create a rich text representation for semantic search
        document = (
            f"Employee Name: {name}. "
            f"Team: {team}. "
            f"Designation: {designation}. "
            f"Phone Number: {phone_number}. "
            f"Technology/Skills: {technology}."
        )
        
        # Use a deterministic ID (e.g. based on name and team)
        doc_id = f"emp_{name.lower().replace(' ', '_')}_{team.lower().replace(' ', '_')}"
        
        metadata = {
            "name": name,
            "designation": designation,
            "phone_number": phone_number,
            "team": team,
            "technology": technology
        }
        
        self.collection.upsert(
            ids=[doc_id],
            documents=[document],
            metadatas=[metadata]
        )
        return doc_id

    def clear_database(self):
        """
        Deletes all documents from the collection.
        """
        try:
            self.collection.delete()
        except Exception as e:
            pass

    def search_employee(self, query: str, limit: int = 3):
        """
        Searches for an employee based on a query like "forward to Shubham in python team".
        """
        results = self.collection.query(
            query_texts=[query],
            n_results=limit
        )
        
        # Format the results into a list of dicts
        employees = []
        if results and results["metadatas"] and len(results["metadatas"]) > 0:
            metadatas = results["metadatas"][0]
            documents = results["documents"][0]
            distances = results["distances"][0] if "distances" in results else [0.0] * len(metadatas)
            
            for i in range(len(metadatas)):
                employees.append({
                    "metadata": metadatas[i],
                    "document": documents[i],
                    "distance": distances[i]
                })
        
        return employees
