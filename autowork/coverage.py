"""Requirement coverage: what a job description asks for that the resume lacks.

This is the cheap half of the two-layer idea. It models the *literal* filter —
the keyword and boolean search an ATS runs before a human sees anything — not
the judgement a screener applies afterwards. Those fail differently: the ATS
drops you for missing the exact string, a screener drops you for thin evidence.
Only the second needs an LLM, and this handles the first for free.

Aliases matter more than they look. A resume saying "Kubernetes" and a JD saying
"K8s" are the same skill to a human and different strings to a search box, so
every term carries its surface forms and a hit on any one counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Canonical term -> surface forms seen in job descriptions. Only the canonical
# name is ever reported; the variants exist to catch it being written another way.
VOCABULARY: dict[str, tuple[str, ...]] = {
    # languages
    "Go": ("golang", "go lang"),
    "Python": (),
    "Java": (),
    "JavaScript": ("js", "ecmascript"),
    "TypeScript": ("ts",),
    "Ruby": (),
    "Rust": (),
    "C++": ("cpp",),
    "C#": ("csharp", ".net"),
    "Kotlin": (),
    "Scala": (),
    "PHP": (),
    "Swift": (),
    "Elixir": (),
    "SQL": (),
    "Bash": ("shell scripting", "shell script"),
    # backend frameworks
    "Ruby on Rails": ("rails",),
    "Django": (),
    "Flask": (),
    "FastAPI": (),
    "Spring Boot": ("spring",),
    "Node.js": ("nodejs", "node js", "express.js", "expressjs"),
    "GraphQL": (),
    "gRPC": ("grpc",),
    "REST APIs": ("rest api", "restful", "rest"),
    "Microservices": ("micro-services",),
    # frontend
    "React": ("react.js", "reactjs"),
    "Vue": ("vue.js", "vuejs", "vue 3"),
    "Angular": (),
    "Next.js": ("nextjs",),
    "Svelte": (),
    "Tailwind": ("tailwindcss",),
    # cloud
    "AWS": ("amazon web services",),
    "GCP": ("google cloud", "google cloud platform"),
    "Azure": ("microsoft azure",),
    "Kubernetes": ("k8s", "eks", "gke", "aks"),
    "Docker": ("containerisation", "containerization"),
    "Terraform": ("hcl",),
    "Pulumi": (),
    "Ansible": (),
    "Helm": (),
    "Serverless": ("lambda", "cloud functions"),
    "CI/CD": ("continuous integration", "continuous delivery", "continuous deployment"),
    "Jenkins": (),
    "GitHub Actions": ("gh actions",),
    "GitLab CI": ("gitlab-ci",),
    "Argo CD": ("argocd", "gitops"),
    "Linux": ("unix",),
    "Nginx": (),
    # data
    "PostgreSQL": ("postgres", "psql"),
    "MySQL": (),
    "MongoDB": ("mongo",),
    "Redis": (),
    "Elasticsearch": ("opensearch", "elastic search"),
    "Kafka": ("event streaming", "msk"),
    "RabbitMQ": (),
    "Snowflake": (),
    "Spark": ("pyspark", "apache spark"),
    "Airflow": ("apache airflow",),
    "dbt": (),
    "ETL": ("elt", "data pipeline"),
    "Data Warehouse": ("warehousing",),
    "ClickHouse": (),
    "DynamoDB": (),
    "Cassandra": (),
    # observability & reliability
    "Prometheus": (),
    "Grafana": (),
    "Datadog": (),
    "Observability": ("monitoring", "telemetry", "opentelemetry"),
    "On-call": ("oncall", "incident response", "pagerduty"),
    "SLO": ("sli", "slas", "error budget"),
    "Load Testing": ("performance testing", "benchmarking"),
    # ai / ml
    "LLM": ("large language model", "gpt", "llms"),
    "RAG": ("retrieval augmented", "retrieval-augmented"),
    "AI Agents": ("agentic", "ai agent", "tool use", "function calling", "mcp"),
    "Prompt Engineering": ("prompting",),
    "PyTorch": (),
    "TensorFlow": (),
    "Vector Database": ("pinecone", "weaviate", "qdrant", "embeddings"),
    "MLOps": ("ml ops", "model serving"),
    # practice
    "Testing": ("unit test", "unit tests", "integration test", "tdd", "rspec", "pytest", "jest"),
    "Git": ("version control",),
    "Agile": ("scrum", "kanban"),
    "Code Review": ("peer review",),
    "System Design": ("distributed systems", "scalability", "high availability"),
    "Security": ("appsec", "owasp", "authentication", "authorization"),
}

# Sections where a JD states what it actually requires. Terms only mentioned in
# the company blurb are noise, not requirements.
_REQ_SECTION = re.compile(
    r"(requirements?|qualifications?|what (?:you|we)(?:'| a)?re looking for|"
    r"must[- ]haves?|you (?:should )?have|skills?|experience with|"
    r"basic qualifications|minimum qualifications|about you|who you are)",
    re.I,
)


def _present(term: str, variants: tuple[str, ...], text: str) -> bool:
    for form in (term, *variants):
        # C++ and C# contain regex metacharacters, and a bare \b after '+' or
        # '#' never matches — guard on a non-word character instead.
        pattern = (
            rf"(?<![\w]){re.escape(form.lower())}(?![\w])"
            if form[-1].isalnum()
            else rf"(?<![\w]){re.escape(form.lower())}"
        )
        if re.search(pattern, text):
            return True
    return False


def _requirements_text(description: str) -> str:
    """Prefer the requirements half of the description when it is findable.

    Falls back to the whole text: a JD with no recognisable section headings
    should still be scanned rather than silently returning no requirements.
    """
    match = _REQ_SECTION.search(description)
    return description[match.start():].lower() if match else description.lower()


@dataclass(slots=True)
class Coverage:
    have: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.have) + len(self.missing)

    @property
    def ratio(self) -> float:
        return len(self.have) / self.total if self.total else 1.0

    def summary(self, limit: int = 5) -> str:
        if not self.total:
            return "no explicit requirements detected"
        pct = round(self.ratio * 100)
        if not self.missing:
            return f"{pct}% — covers every term detected"
        return f"{pct}% — missing: {', '.join(self.missing[:limit])}"


def candidate_terms(cfg: dict) -> set[str]:
    """Canonical vocabulary terms the resumes evidence."""
    owned: set[str] = set()
    resume_text = " ".join(
        skill for profile in cfg["profiles"].values() for skill in profile["skills"]
    ).lower()
    for term, variants in VOCABULARY.items():
        if _present(term, variants, resume_text):
            owned.add(term)
    return owned


def analyse(description: str | None, owned: set[str]) -> Coverage:
    if not description:
        return Coverage()
    text = _requirements_text(description)
    result = Coverage()
    for term, variants in VOCABULARY.items():
        if not _present(term, variants, text):
            continue
        (result.have if term in owned else result.missing).append(term)
    return result
