# Transcript Intelligence

**B2B Enterprise SaaS · Automated Call Transcript Analysis Pipeline**
*Take-home assignment*

---

## What This Is

Transcript Intelligence automatically processes call transcripts across three call types — customer support, external account calls, and internal meetings and surfaces actionable intelligence for four stakeholder groups: Customer Success, Support Ops, Product Management, and Engineering Leadership.

## Architecture

**What this shows:** the full data flow from raw transcript drop in S3 through to dashboard render. The left side is the ingestion + LLM analysis path (S3 → SQS → PII redaction → Bedrock Agent → DynamoDB). The right side is the analytics + access layer (DynamoDB Export → Athena → QuickSight, plus the API Gateway endpoints for chatbot, feedback, and dashboard embedding). The MCP server sits on top of DynamoDB as a parallel query interface for analyst tooling.

**Key design choices visible here:** PII redaction happens *before* the LLM call (not after) so no customer PII ever reaches Bedrock. The eval suite imports directly from Lambda source so the same code path runs in production and in CI.

<img width="861" height="667" alt="image" src="https://github.com/user-attachments/assets/22442e5e-6de1-4290-805a-f2f2f4f19dc5" />


**Five key findings from the dataset:**
| # | Finding | Impact |
|---|---|---|
| 1 | One incident drove 36% of all call volume | March 2026 Detect pipeline outage is the dominant signal |
| 2 | Feb→Mar sentiment drop: -0.90 pts (4.2σ, p<0.0001) | Statistically significant - real event, not variance |
| 3 | 10 enterprise accounts at churn risk | 3 in active competitive evaluations |
| 4 | Compliance & Audit avg sentiment 4.40 — highest category | Near-term expansion revenue opportunity |
| 5 | Feature gaps in 49% of all meetings | Voice-of-customer roadmap signal hidden in plain sight |

---
**DATASET & SCOPE**
<img width="902" height="333" alt="image" src="https://github.com/user-attachments/assets/dcb7026e-8ece-46da-8510-f8c03ae81a7c" />
**What this shows:** the shape of the input data. 100 meetings, three call types (support / external / internal), spread across a 6-month window from Oct 2025 to Mar 2026.

**What to look for:** the call-type mix is balanced so no single type dominates the analysis. The time window is wide enough to compute a meaningful baseline (months 2-4) and detect deviation (month 3 - March).

**Category Results**
<img width="485" height="267" alt="image" src="https://github.com/user-attachments/assets/a669465e-0ba3-4c93-9d0c-a910bf5218e2" />

The first chart shows the distribution of meetings across primary categories.

- **Technical Support** was the largest category, with **30 meetings**.
- Other major categories included:
  - **Internal Planning & Engineering**: 13 meetings
  - **Commercial & Renewal**: 12 meetings
  - **Incident Response**: 11 meetings
  - **Compliance & Audit**: 10 meetings
- Smaller categories included Account Health & QBR, Product Feedback & Roadmap, Onboarding & Deployment, and Competitive Intelligence.

This indicates that support-related conversations made up the largest share of the analyzed meeting set.

### Sentiment Analysis
<img width="881" height="274" alt="image" src="https://github.com/user-attachments/assets/e7b088b9-7417-4e75-a575-42d9383e8276" />
The sentiment distribution compares actual meeting sentiment against baseline expectations for three call types:

- **Support Calls**: 27 meetings  
  - Average sentiment: **2.94**
  - Matches the baseline sentiment of **2.94**
  - Sentiment is generally neutral to slightly negative.

- **External Calls**: 40 meetings  
  - Average sentiment: **3.85**
  - Below the baseline of **4.21**
  - External calls remain relatively positive, but slightly under expected sentiment.

- **Internal Calls**: 33 meetings  
  - Average sentiment: **3.28**
  - Slightly below the baseline of **3.42**
  - Internal sentiment shows more variation across meetings.

### The March Story
<img width="922" height="281" alt="image" src="https://github.com/user-attachments/assets/1a0b0378-61c8-49d8-8930-51d1a3cf29f7" />
The monthly trend chart highlights a clear sentiment drop in March.

- Overall sentiment decreased from **3.68 in February** to **2.78 in March**.
- The chart flags a **Detect Pipeline Outage on March 10**, which appears to be associated with the March sentiment decline.
- Sentiment recovered in April, rising to **3.83**.
- External calls remained the strongest sentiment category overall, while support calls stayed lower but more stable.

This suggests March was an anomalous month, likely impacted by the pipeline outage, followed by recovery in April.

### Anamolies
<img width="504" height="395" alt="image" src="https://github.com/user-attachments/assets/53a2f641-2ef6-4963-808e-89a78eb98068" />
The anomaly detection chart uses sentiment z-scores to identify meetings that significantly deviated from normal sentiment patterns.

- The normal range is shown between approximately **-1.5 and +1.5 z-score**.
- Meetings outside this range are marked as anomalies.
- Total anomalies detected: **25 out of 100 meetings**
- By call type:
  - **Support**: 3 anomalies out of 27 meetings
  - **External**: 10 anomalies out of 40 meetings
  - **Internal**: 12 anomalies out of 33 meetings

Internal calls had the highest number of anomalies, suggesting greater sentiment volatility in internal discussions. External calls also showed several anomalies, while support calls were comparatively more consistent.

### ACCOUNT CHURN RISK RADAR
<img width="498" height="396" alt="image" src="https://github.com/user-attachments/assets/e07b8170-410e-45fa-bbbf-4baeacc9dfe8" />

This chart identifies **10 enterprise accounts with elevated churn risk** based on low sentiment scores.

#### Key Findings

- All 10 accounts are below the **risk threshold of 3.0**, meaning they should be reviewed for potential churn risk.
- The lowest sentiment account is **Northstar Pharma**, with a score of **2.1** and a z-score of **-2.64**.
- Other high-risk accounts include:
  - **Helix Data**: 2.3
  - **Summit Trust**: 2.4
  - **Meridian Capital**: 2.4
  - **Quantum Edge**: 2.4
- Several accounts are marked in red because they are in **active competitive evaluation**, increasing churn risk:
  - **Ironworks Corp**
  - **Brightpath Commerce**
  - **Quantum Edge**

## OUTAGE BLAST RADIUS TRACKING
<img width="512" height="423" alt="image" src="https://github.com/user-attachments/assets/bc598e25-2219-43fb-899e-3636ccf97a6a" />
This chart tracks the sentiment impact and meeting volume related to the **Detect Outage** from **March 10 through April 2026**.
### 1. Sentiment Timeline

The top chart shows sentiment scores over time for three call types:

- **Support calls** are shown in blue.
- **External calls** are shown in green.
- **Internal calls** are shown in red.

Two important milestones are marked:

- **Red dashed line**: Outage start on **March 10**
- **Orange dashed line**: Post-incident review on **March 18**
- **Gray dotted line**: Sentiment risk threshold at **3.0**

### Key Observations

- Sentiment dropped sharply around the outage start date.
- Several internal and support meetings fell below the **3.0 sentiment threshold** immediately after March 10.
- External sentiment also weakened during the incident window but recovered more strongly later in March and April.
- After the March 18 post-incident review, sentiment began to improve, especially in external conversations.
- April shows recovery, but sentiment remains volatile, with some meetings still falling below the risk threshold.

### 2. Outage-Related Meeting Volume

The bottom chart shows outage-related meeting volume by month.

| Month | Support | External | Internal | Total |
|---|---:|---:|---:|---:|
| February 2026 | 4 | 2 | 1 | 7 |
| March 2026 | 6 | 9 | 11 | 26 |
| April 2026 | 4 | 9 | 9 | 22 |

March had the highest outage-related meeting volume, with **26 meetings**, reflecting the peak impact period of the incident. April remained elevated with **22 meetings**, showing that outage follow-up and customer impact continued beyond the initial incident month.

## Interpretation

The outage had a clear blast radius across support, external, and internal conversations. The most severe sentiment impact occurred around the March 10 outage start, followed by a gradual recovery after the March 18 post-incident review.

Internal meetings showed the highest volume during March, suggesting significant operational coordination and incident response activity. External meetings remained elevated in April, indicating continued customer follow-up, trust rebuilding, and account management work.


## FEATURE GAP INTELLIGENCE**
<img width="490" height="423" alt="image" src="https://github.com/user-attachments/assets/d84c8337-9676-470b-a1b0-63a5cef77726" />
## Feature Gap Intelligence

This dashboard highlights **feature-gap signals from 49 meetings**. It identifies the most common customer or internal roadmap-related topics and shows where feature gaps appear most frequently by call type and category.

### 1. Top Feature-Gap Topics

The left chart shows the most frequently mentioned feature-gap topics across meetings.

The most common topics were:

| Topic | Mentions |
|---|---:|
| Compliance reporting | 10 |
| Renewal | 8 |
| Compliance | 8 |
| Feature request | 6 |
| Identity management | 5 |
| Churn risk | 5 |
| Onboarding | 5 |
| Product roadmap | 5 |
| Backup and recovery | 3 |
| Service outage | 3 |
| Outage post-mortem | 3 |
| PCI DSS | 3 |
| Sprint retrospective | 3 |
| Incident response | 3 |
| Product feedback | 3 |

### 2. Feature Gap Rate by Call Type and Category

The heatmap shows the percentage of meetings in each call type and category that contained a feature-gap signal.

Key patterns include:

- **External calls** show strong feature-gap signals in:
  - Product Feedback & Roadmap: **100%**
  - Technical Support: **100%**
  - Commercial & Renewal: **67%**
  - Onboarding & Deployment: **67%**
  - Compliance & Audit: **50%**

- **Internal calls** show feature-gap signals in:
  - Account Health & QBR: **100%**
  - Internal Planning & Engineering: **62%**
  - Competitive Intelligence: **50%**
  - Compliance & Audit: **50%**
  - Product Feedback & Roadmap: **50%**
  - Technical Support: **50%**

- **Support calls** show fewer feature-gap signals overall, with the clearest signal in:
  - Technical Support: **37%**

### Interpretation

Feature gaps are most visible in external customer-facing conversations, especially around roadmap discussions, technical support, onboarding, renewals, and compliance. This suggests that customers are frequently connecting product limitations or missing capabilities to commercial decisions, implementation needs, and support experiences.

Internal conversations also surface feature gaps, especially in account health reviews and engineering planning, indicating that teams are translating customer pain points into roadmap or operational discussions.

## Key Takeaways

- Compliance-related needs are the strongest recurring feature-gap theme.
- Product roadmap and technical support conversations show high feature-gap rates.
- External calls provide the strongest voice-of-customer roadmap signal.
- Internal planning discussions are also capturing feature gaps, especially around account health and engineering priorities.
- Support calls show fewer broad feature-gap signals, but technical support remains an important source of product feedback.

## Recommended Actions

- Prioritize review of compliance reporting, compliance, and identity management requests.
- Connect feature-gap signals to renewal and churn-risk workflows.
- Use external Product Feedback & Roadmap and Technical Support calls as high-value sources for roadmap planning.
- Share recurring customer feature gaps with product, engineering, customer success, and sales teams.
  
## Escalation
<img width="498" height="235" alt="image" src="https://github.com/user-attachments/assets/4b0f000c-6884-4fe6-82d1-24b98e322cd5" />
## Escalation

This dashboard summarizes **automated escalation routing for 64 meetings requiring action**, including both **P0 critical** and **P1 high-priority** escalations.

### 1. Escalation Destination by Team

The left chart shows which teams received escalated meetings.

| Team | Meetings Routed |
|---|---:|
| Engineering | 56 |
| Customer Success | 5 |
| Security | 3 |

Engineering received the majority of escalations, with **56 out of 64 meetings** routed to that team. This suggests most action items were related to technical investigation, product issues, incident response, or engineering follow-up.

Customer Success received **5 escalations**, likely tied to customer communication, account management, or follow-up actions.

Security received **3 escalations**, indicating a smaller number of meetings involved security-related concerns or required security team review.

### 2. Priority Breakdown

The right chart shows the severity level of the escalated meetings.

| Priority | Meaning | Meetings |
|---|---|---:|
| P0 | Critical | 56 |
| P1 | High | 8 |

Most escalations were classified as **P0 critical**, representing **56 meetings**. The remaining **8 meetings** were classified as **P1 high priority**.

### Interpretation

The escalation system identified a large number of meetings requiring urgent follow-up, with the majority routed to Engineering and marked as P0. This indicates that the most common escalation path is technical and time-sensitive.

The smaller number of Customer Success and Security escalations suggests that most issues were not primarily relationship-management or security-driven, although those teams still had a role in selected cases.

## Key Takeaways

- **64 meetings required escalation action.**
- **Engineering handled 56 escalations**, making it the primary destination team.
- **P0 critical escalations accounted for 56 meetings**, showing a strong concentration of urgent issues.
- **P1 escalations accounted for 8 meetings**, representing high-priority but less critical follow-ups.
- Customer Success and Security received fewer escalations but remain important for customer and risk-related follow-up.

## Recommended Actions

- Review the Engineering escalation queue to identify recurring technical themes.
- Validate whether P0 classification is being applied consistently.
- Track resolution time for P0 and P1 escalations separately.
- Share escalation trends with Engineering, Customer Success, and Security leadership.
- Investigate whether repeated Engineering escalations point to systemic product or infrastructure issues.

## Start Here

```bash
# Install dependencies
pip install -r requirements-dev.txt

# Open the analysis notebook
jupyter notebook notebooks/transcript_intelligence_analysis.ipynb

# Run the eval suite (36 ground-truth cases, no mocks)
python scripts/eval/eval_runner.py
```

---

## Repository Structure

```
├── notebooks/
│   └── transcript_intelligence_analysis.ipynb   # Full analysis — start here
│
├── data/dataset/                                 # 100 synthetic meeting folders
│   └── <meeting-id>/
│       ├── meeting-info.json                     # Title, attendees, timestamps
│       ├── summary.json                          # Sentiment, topics, key moments
│       └── transcript.json                       # Raw transcript segments
│
├── lambda/
│   ├── agent_actions/handler.py                  # 6 tool functions (classify, analyse, route…)
│   ├── orchestrator/handler.py                   # Bedrock Agent SDK orchestrator
│   ├── ingestion_pii/handler.py                  # PII redaction before any LLM call
│   ├── chatbot/handler.py                        # BM25-based RAG chatbot (POST /chat)
│   ├── feedback/handler.py                       # Human-in-the-loop corrections (POST /feedback)
│   └── shared/python/
│       ├── ti_config.py                          # Single source of truth: taxonomy, rules, baselines
│       ├── ti_shared.py                          # TokenTracker, StructuredLogger
│       └── ti_classifier.py                      # Call-type classifier
│
├── mcp_server/server.py                          # Custom MCP server (4 tools over DynamoDB)
│
├── scripts/eval/
│   ├── eval_runner.py                            # Regression eval harness — CI-ready
│   └── eval_cases.json                           # 36 ground-truth test cases
│
├── transcript_intelligence/
│   └── transcript_intelligence_stack.py          # AWS CDK infrastructure stack
│
└── config/app_config.py                          # Infrastructure-level configuration
```

---

## Pipeline Architecture

```
S3 (raw transcripts)
  └── s3_trigger Lambda
        └── SQS → ingestion_pii Lambda
                    ├── PII redaction (regex patterns, Presidio-compatible)
                    └── Bedrock Agent (Claude Sonnet 4)
                              ├── classify_call_type   → support / external / internal
                              ├── classify_category    → 9 Business Moment categories
                              ├── analyze_sentiment    → z-score vs per-type baseline
                              ├── extract_issues       → severity p0–p3
                              ├── route_escalation     → engineering / security / cs / account
                              └── save_record          → DynamoDB (ti-records)
                                                         │   fields: account_name, feature_gap, month
                                                         └── SNS → escalation alerts

QuickSight analytics layer
  DynamoDB Export → S3 → Glue Crawler → Athena → QuickSight (DIRECT_QUERY)
    Dashboard: 2 sheets · 11 visuals
      ├── Overview:  sentiment KPIs, call-mix, category matrix, escalation heatmap
      └── Insights:  March story, anomaly scatter, churn risk, feature gaps, blast radius

API Gateway
  ├── POST /chat       → chatbot Lambda (BM25 RAG over ti-records)
  ├── POST /feedback   → feedback Lambda (corrections → ti-feedback table)
  └── GET  /dashboard  → qs_embed Lambda (anonymous QuickSight embed URL)

MCP Server (FastMCP, 4 tools)
  ├── search_transcripts
  ├── get_meeting_insights
  ├── get_sentiment_trends
  └── get_anomalies
```

---

## Categorisation: 9 Business Moment Categories

The pipeline uses a 3-level hybrid classifier — faster and more auditable than LLM classification for this domain:

| Level | Method | Fires When |
|---|---|---|
| L1 | Title regex | Meeting title matches a known pattern (e.g. `^Support Case`, `Detect Outage -`) |
| L2 | Primary topic keyword | No title match; first/primary topic matches a category keyword |
| L3 | Full topic set | Fallback across all extracted topics |

| Category | Avg Sentiment | Sample Meetings |
|---|---|---|
| Incident Response | 2.11 | War rooms, customer impact assessments, outage escalations |
| Technical Support | 3.02 | Support case tickets, bug investigations, API issues |
| Compliance & Audit | 4.40 | SOC 2 prep, ISO 27001, HIPAA reviews |
| Commercial & Renewal | 3.71 | Renewal discussions, contract reviews, Q-planning |
| Account Health & QBR | 3.03 | Business reviews, service reliability discussions |
| Competitive Intelligence | 2.86 | Win/loss analysis, competitive evaluations |
| Product Feedback & Roadmap | 3.92 | Roadmap reviews, feature requests, early access demos |
| Onboarding & Deployment | 4.67 | Kickoffs, identity module setups, SAML/SSO deployments |
| Internal Planning & Engineering | 4.02 | Standups, sprint planning, post-mortems, all-hands |

---

## Eval Framework

36 ground-truth cases across 5 deterministic tools. Tests import directly from Lambda source — no mocks.

```bash
python scripts/eval/eval_runner.py                        # all 36 cases
python scripts/eval/eval_runner.py --tool classify_category
python scripts/eval/eval_runner.py --fail-fast            # CI mode
python scripts/eval/eval_runner.py --json-out report.json
```

Result: **36/36 (100%)** — suitable as a CI regression gate.

---

## AWS Deployment (optional)

```bash
export CDK_DEFAULT_ACCOUNT=<your-account-id>
export CDK_DEFAULT_REGION=us-east-1
pip install -r requirements.txt
cdk deploy
```
