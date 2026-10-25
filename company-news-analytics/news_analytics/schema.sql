PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT OR IGNORE INTO schema_version VALUES(1);

-- Immutable input text: edits obtain a different input_id. Vectors are a resumable cache.
CREATE TABLE IF NOT EXISTS embedding_inputs(
 id INTEGER PRIMARY KEY,
 role TEXT NOT NULL CHECK(role IN ('company_query','news_context','news_mechanism','analytics_source','analytics_mechanism')),
 text TEXT NOT NULL CHECK(length(trim(text))>0), text_hash TEXT NOT NULL,
 encoding_method TEXT NOT NULL CHECK(encoding_method IN ('encode_query','encode_document')),
 embedding BLOB, model_id TEXT, model_revision TEXT, dimensions INTEGER,
 normalized INTEGER, embedded_input_hash TEXT, prompt TEXT, token_count INTEGER, embedded_at TEXT,
 UNIQUE(role,text_hash),
 CHECK((role IN ('company_query','news_mechanism') AND encoding_method='encode_query') OR
       (role IN ('news_context','analytics_source','analytics_mechanism') AND encoding_method='encode_document')),
 CHECK(embedding IS NULL OR (length(embedding)=3072 AND dimensions IS NOT NULL AND dimensions=768 AND normalized IS NOT NULL AND normalized=1
       AND model_id IS NOT NULL AND model_revision IS NOT NULL AND embedded_input_hash=text_hash
       AND embedded_input_hash IS NOT NULL AND prompt IS NOT NULL AND token_count IS NOT NULL AND token_count BETWEEN 1 AND 2048 AND embedded_at IS NOT NULL))
);
CREATE TRIGGER IF NOT EXISTS immutable_embedding_input
 BEFORE UPDATE OF text,text_hash,role,encoding_method ON embedding_inputs
 WHEN NEW.text IS NOT OLD.text OR NEW.text_hash IS NOT OLD.text_hash OR NEW.role IS NOT OLD.role OR NEW.encoding_method IS NOT OLD.encoding_method
 BEGIN SELECT RAISE(ABORT,'Embedding inputs are immutable; create a new input'); END;

CREATE TABLE IF NOT EXISTS companies(
 id TEXT PRIMARY KEY, profile_text TEXT NOT NULL, facts_json TEXT NOT NULL,
 source_json TEXT NOT NULL, input_hash TEXT NOT NULL, input_file TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_queries(
 company_id TEXT NOT NULL REFERENCES companies(id),query_id TEXT NOT NULL,
 query_index INTEGER NOT NULL,channel TEXT NOT NULL,weight REAL NOT NULL CHECK(weight>=0),
 input_id INTEGER NOT NULL REFERENCES embedding_inputs(id),metadata_json TEXT NOT NULL,
 is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),PRIMARY KEY(company_id,query_id)
);
CREATE TABLE IF NOT EXISTS news(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,url TEXT NOT NULL UNIQUE,publisher TEXT,
 publication_date TEXT NOT NULL,source_json TEXT NOT NULL,input_hash TEXT NOT NULL,
 input_file TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS article_chunks(
 id TEXT PRIMARY KEY,news_id TEXT NOT NULL REFERENCES news(id),local_id TEXT NOT NULL,
 chunk_index INTEGER NOT NULL,thesis TEXT NOT NULL,context TEXT NOT NULL,text TEXT NOT NULL,
 context_input_id INTEGER NOT NULL REFERENCES embedding_inputs(id),
 mechanism_input_id INTEGER REFERENCES embedding_inputs(id),metadata_json TEXT NOT NULL,
 is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),UNIQUE(news_id,local_id)
);
CREATE TABLE IF NOT EXISTS analytics(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,publisher TEXT,document_date TEXT,document_period TEXT,
 source_sha256 TEXT NOT NULL,source_path TEXT,source_json TEXT NOT NULL,input_hash TEXT NOT NULL,
 input_file TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics_pages(
 analytics_id TEXT NOT NULL REFERENCES analytics(id),page_number INTEGER NOT NULL,
 text TEXT NOT NULL,metadata_json TEXT NOT NULL,PRIMARY KEY(analytics_id,page_number)
);
CREATE TABLE IF NOT EXISTS analytics_visuals(
 analytics_id TEXT NOT NULL REFERENCES analytics(id),local_id TEXT NOT NULL,page_number INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('chart','table')),title TEXT NOT NULL,metadata_json TEXT NOT NULL,
 PRIMARY KEY(analytics_id,local_id),FOREIGN KEY(analytics_id,page_number) REFERENCES analytics_pages(analytics_id,page_number)
);
CREATE TABLE IF NOT EXISTS analytics_chunks(
 id TEXT PRIMARY KEY,analytics_id TEXT NOT NULL REFERENCES analytics(id),local_id TEXT NOT NULL,
 chunk_index INTEGER NOT NULL,title TEXT NOT NULL,text TEXT NOT NULL,pages_json TEXT NOT NULL,
 source_input_id INTEGER NOT NULL REFERENCES embedding_inputs(id),metadata_json TEXT NOT NULL,
 is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),UNIQUE(analytics_id,local_id),UNIQUE(analytics_id,id)
);
CREATE TABLE IF NOT EXISTS analytics_mechanisms(
 id TEXT PRIMARY KEY,analytics_id TEXT NOT NULL REFERENCES analytics(id),title TEXT NOT NULL,
 input_id INTEGER NOT NULL REFERENCES embedding_inputs(id),metadata_json TEXT NOT NULL,
 is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),UNIQUE(analytics_id,id)
);
CREATE TABLE IF NOT EXISTS analytics_mechanism_sources(
 mechanism_id TEXT NOT NULL,analytics_id TEXT NOT NULL,chunk_id TEXT NOT NULL,evidence_json TEXT NOT NULL,
 PRIMARY KEY(mechanism_id,chunk_id),
 FOREIGN KEY(analytics_id,mechanism_id) REFERENCES analytics_mechanisms(analytics_id,id),
 FOREIGN KEY(analytics_id,chunk_id) REFERENCES analytics_chunks(analytics_id,id)
);
CREATE VIEW IF NOT EXISTS active_embedding_inputs AS
 SELECT DISTINCT e.* FROM embedding_inputs e JOIN (
 SELECT input_id FROM company_queries WHERE is_active=1 AND weight>0
 UNION SELECT context_input_id FROM article_chunks WHERE is_active=1
 UNION SELECT mechanism_input_id FROM article_chunks WHERE is_active=1 AND mechanism_input_id IS NOT NULL
 UNION SELECT source_input_id FROM analytics_chunks WHERE is_active=1
 UNION SELECT input_id FROM analytics_mechanisms WHERE is_active=1
 ) refs ON refs.input_id=e.id;

CREATE TABLE IF NOT EXISTS comparison_runs(
 id TEXT PRIMARY KEY,created_at TEXT NOT NULL,as_of TEXT NOT NULL,
 settings_json TEXT NOT NULL,model_json TEXT NOT NULL,status TEXT NOT NULL,
 import_summary_json TEXT NOT NULL,summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_reports(
 run_id TEXT NOT NULL REFERENCES comparison_runs(id),company_id TEXT NOT NULL REFERENCES companies(id),
 report_json TEXT NOT NULL,PRIMARY KEY(run_id,company_id)
);

-- Results of company/news checks survive report limits and process restarts.
-- A new content revision is a different pair. Unrelated news imports do not
-- invalidate completed checks; analytical documents are searched on dispatch.
CREATE TABLE IF NOT EXISTS company_news_checks(
 company_id TEXT NOT NULL REFERENCES companies(id),company_revision TEXT NOT NULL,
 news_id TEXT NOT NULL REFERENCES news(id),news_revision TEXT NOT NULL,
 scoring_revision TEXT NOT NULL,compared_at TEXT NOT NULL,company_score REAL,
 disposition TEXT NOT NULL CHECK(disposition IN ('queued','not_relevant')),
 candidate_json TEXT,assigned_run_id TEXT REFERENCES comparison_runs(id),
 PRIMARY KEY(company_id,company_revision,news_id,news_revision,scoring_revision)
);
CREATE TABLE IF NOT EXISTS company_report_batches(
 run_id TEXT NOT NULL,company_id TEXT NOT NULL,
 company_revision TEXT NOT NULL,scoring_revision TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','completed','cancelled')),
 created_at TEXT NOT NULL,completed_at TEXT,result_json TEXT,saved_json TEXT NOT NULL,
 last_error TEXT,failed_attempts INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(run_id,company_id),
 FOREIGN KEY(run_id,company_id) REFERENCES company_reports(run_id,company_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_prepared_company_batch
 ON company_report_batches(company_id) WHERE status='prepared';
CREATE INDEX IF NOT EXISTS idx_company_checks_pending
 ON company_news_checks(company_id,company_revision,scoring_revision,assigned_run_id);
CREATE INDEX IF NOT EXISTS idx_article_chunks_news ON article_chunks(news_id,is_active);
CREATE INDEX IF NOT EXISTS idx_analytics_chunks_doc ON analytics_chunks(analytics_id,is_active);
CREATE INDEX IF NOT EXISTS idx_mechanisms_doc ON analytics_mechanisms(analytics_id,is_active);
CREATE INDEX IF NOT EXISTS idx_queries_company ON company_queries(company_id,is_active);
