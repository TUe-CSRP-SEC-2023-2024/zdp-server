import sqlite3
from datetime import datetime
import tldextract


class Sessions():
    """
    The storage interface for all current sessions.

    Note that, currently, once a session has completed, it will not be deleted from the database.
    """
    shared = False
    storage = None

    def __init__(self, db_file_path, shared=False):
        self.shared = shared
        self.storage = db_file_path

        storage_conn = sqlite3.connect(db_file_path)        
        self._setup_storage(storage_conn)

        # Delete unfinished tasks on start-up
        storage_conn.execute("DELETE FROM session WHERE result = 'processing'")
        storage_conn.commit()
        storage_conn.close()

    def store_state(self, uuid, url, result, state):
        """
        Stores the given state (uuid, url, result, state) in the database.
        """
        storage_conn = sqlite3.connect(self.storage)
        
        now = datetime.now()
        if self.get_state(uuid, url) == 'new':
            domain = tldextract.extract(url).registered_domain
            
            storage_conn.execute("INSERT INTO session (uuid, timestamp, url, tld, result, state) VALUES (?, ?, ?, ?, ?, ?)", 
                                 [uuid, now, url, domain, result, state])
        else:
            storage_conn.execute("UPDATE session SET result = ?, timestamp = ?, state = ? WHERE uuid = ? AND url = ?", 
                                 [result, now, state, uuid, url])
        
        storage_conn.commit()
        storage_conn.close()

    def get_state(self, uuid, url):
        """
        Retrieves the current state from the database, or 'new' in case it is not present.
        """
        storage_conn = sqlite3.connect(self.storage)
        
        if self.shared:
            cursor = storage_conn.execute("SELECT result, state, timestamp FROM session WHERE url = ?", [url])
        else:
            cursor = storage_conn.execute("SELECT result, state, timestamp FROM session WHERE uuid = ? AND url = ?", [uuid, url])
        
        result = cursor.fetchone()
        storage_conn.close()
        if result == None:
            return 'new'
        else:
            return result

    def _setup_storage(self, storage_conn):
        sql_q_db = '''
            CREATE TABLE IF NOT EXISTS "session" (
                "uuid"	string,
                "timestamp" string,
                "url"	string,
                "tld"	string,
                "result" string,
                "state" string
            );'''
        storage_conn.execute(sql_q_db)
        storage_conn.commit()
