# fetchJobsForMe

A personal CSE/IT job finder. It fetches jobs from public job-board pages and APIs, filters them by each user's search preferences, keeps saved jobs and a parsed resume per user in Firebase, and reviews the resume with Gemini.

The diagrams below are Mermaid. GitHub and the Cursor Markdown preview draw them from this file, so they change as soon as this file changes.

## Architecture

```mermaid
flowchart LR
    user([User in browser])

    subgraph web["Frontend · React + Vite · localhost:8002"]
        login[Login]
        pages[Jobs · Saved · Resume Analyzer<br/>Skills & Profile · Settings]
        hooks[useJobs · useSavedJobs<br/>useProfile · usePreferences]
        client[api/client.js<br/>Bearer ID token]
    end

    subgraph auth["Firebase"]
        fbauth[Firebase Auth]
        firestore[(Firestore)]
    end

    subgraph api["Backend · Python ThreadingHTTPServer · 127.0.0.1:8001"]
        server[server.py<br/>routes · preferences · profile · resume parser]
        feeds[feeds.py<br/>portal workers · sidecar store]
        control[control.py<br/>run token · cancel]
        analysis[resume_analysis.py<br/>review · compare · rewrite]
        fbadmin[firebase.py<br/>verify token · Firestore client]
    end

    subgraph portals["Job sources · public pages and APIs"]
        ats[Greenhouse · Lever · Ashby]
        boards[Remotive · RemoteOK · Arbeitnow · Jobicy<br/>Himalayas · TheMuse · WeWorkRemotely<br/>WorkingNomads · FourDayWeek · Shine]
        india[LinkedIn guest · Unstop · Indeed<br/>Naukri visible Chrome · Instahyre]
        adzuna[Adzuna · needs keys]
    end

    gemini[[Google Gemini<br/>Flash models with fallback]]
    disk[(backend/data<br/>one JSON per portal)]

    user --> login --> fbauth
    user --> pages --> hooks --> client
    client -- REST + Bearer token --> server
    hooks -. settings/preferences .-> firestore
    server --> fbadmin --> fbauth
    fbadmin --> firestore
    server --> feeds --> control
    feeds --> ats & boards & india & adzuna
    feeds --> disk
    server --> analysis --> gemini
    analysis --> firestore
```

## Functional map

```mermaid
mindmap
  root((fetchJobsForMe))
    Sign in
      Email and password
      Google
      Expired session logs out
    Jobs
      Live fetch, one at a time
      Stop fetch
      Search with commas meaning OR
      Location, portal, posted date, sort
      Experience cap 0 to 5 years
      India, Indian hybrid, worldwide remote
    Saved jobs
      Star a job
      Remove a job
    Skills and Profile
      Upload PDF, DOCX, TXT
      Layout based parser
      Edit and save profile
    Resume Analyzer
      Score and checklist
      Compare with another resume
      Apply changes
      Download in the same file type
      Token usage line
    Settings
      Search preferences
      Gemini API key
      Portal on and off
      Auto analyse new resume
```

## Job fetch flow

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant P as Jobs page (useJobs)
    participant S as server.py
    participant F as feeds.py workers
    participant J as Job portals
    participant D as backend/data sidecars
    participant FS as Firestore jobSearch

    U->>P: Open Jobs or click Refresh
    P->>S: GET /api/jobs (Bearer token)
    S->>S: Verify token, read preferences
    alt Saved results for these preferences
        S->>FS: Read status and jobs documents
        FS-->>S: Jobs
    else New fetch
        S->>F: Start portals that are not paused
        par Each portal in its own thread
            F->>J: Public page or API request
            J-->>F: Postings
            F->>F: Tech role, last 15 days, India or remote
            F->>D: Write portal JSON
        end
    end
    S->>S: apply_preferences (role, location, experience)
    S-->>P: jobs, portals, loading flag
    loop While loading is true
        P->>S: GET /api/jobs every second
        S-->>P: Cards found so far
    end
    S->>FS: Write status plus jobs, jobs-2 when large
    opt User clicks Stop
        P->>S: GET /api/jobs/stop
        S->>F: Cancel the run token
    end
```

## Resume flow

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as Skills & Profile or Resume Analyzer
    participant S as server.py
    participant R as resume_analysis.py
    participant G as Gemini
    participant FS as Firestore

    U->>UI: Upload PDF, DOCX, or TXT
    UI->>S: POST /api/resume
    S->>S: Extract text (pypdf, then PyMuPDF)
    S->>S: parse_resume: sections, roles, skills, links
    S->>FS: Replace jobProfile/default
    S->>R: remember_resume (text only, not the file)
    R->>FS: Keep previous score, clear old review
    opt Settings has Auto analyse new resume on
        R->>G: Review prompt
        G-->>R: Score, checklist, token usage
        R->>FS: Save resumeAnalysis/resume-analysis
    end
    U->>UI: Analyze or Reanalyse
    UI->>S: POST /api/resume/analyze {force: true}
    S->>R: analyze_resume
    R->>G: Review prompt, next model if busy
    G-->>R: JSON review
    R->>R: Compare with the previous score
    R->>FS: Overwrite resume-analysis
    U->>UI: Apply changes, then Download
    UI->>S: POST /api/resume/rewrite
    R->>G: Edit only the weak lines
    UI->>S: GET /api/resume/download
    S-->>UI: Updated PDF, DOCX, or TXT
```

## Data flow

```mermaid
flowchart TB
    subgraph inputs["Inputs"]
        resumeFile[/Resume file/]
        prefsForm[/Settings form/]
        star[/Star on a job card/]
        portalsIn[/Portal responses/]
    end

    subgraph process["Processing"]
        extract[Text extraction]
        parser[parse_resume]
        crawl[Portal workers]
        filters[Tech role · 15 days · location · experience]
        review[Gemini review and rewrite]
    end

    subgraph store["Firestore · users/{uid}"]
        prefs[(settings/preferences<br/>search fields · pausedPortals<br/>auto_analyse_resume · Gemini key)]
        profile[(jobProfile/default)]
        analysisDoc[(resumeAnalysis/resume-analysis<br/>text · score · rewrite · usage)]
        saved[(savedJobs/saved_jobs)]
        search[(jobSearch/status<br/>jobs · jobs-2 …)]
    end

    sidecar[(backend/data/portal_jobs.json)]
    screens[Job cards · profile · score and issues · download]

    resumeFile --> extract --> parser --> profile
    extract --> analysisDoc
    analysisDoc --> review --> analysisDoc
    prefsForm --> prefs
    prefs --> crawl
    prefs --> filters
    prefs --> review
    portalsIn --> crawl --> sidecar --> filters --> search
    star --> saved
    profile & analysisDoc & saved & search --> screens
```

The Gemini key stays in `settings/preferences`. The preferences API never returns it. Uploaded resume files are not stored, only the extracted text.

## API routes

```mermaid
flowchart LR
    subgraph GET
        g1["/api/health"]
        g2["/api/jobs"]
        g3["/api/jobs/stop"]
        g4["/api/portals"]
        g5["/api/saved"]
        g6["/api/summary"]
        g7["/api/profile"]
        g8["/api/preferences"]
        g9["/api/resume/analysis"]
        g10["/api/resume/download"]
    end
    subgraph POST
        p1["/api/saved"]
        p2["/api/resume"]
        p3["/api/resume/analyze"]
        p4["/api/resume/compare"]
        p5["/api/resume/rewrite"]
        p6["/api/ai/generate"]
    end
    subgraph PUT
        u1["/api/profile"]
        u2["/api/preferences"]
    end
    subgraph DELETE
        d1["/api/saved?link="]
    end
```

Every route except `/api/health` and `/api/portals` reads the user from the `Authorization: Bearer` Firebase ID token.

## Setup

Install the backend packages once:

```powershell
pip install -r backend/requirements.txt
```

Run the commands below from the project folder with Python 3.10 or newer.

## Fetch jobs

Every portal, up to 5 jobs each:

```powershell
python backend/Server/server.py --source all --query engineer --limit 5 --no-open
```

`--source` defaults to `all`, so this is the same search:

```powershell
python backend/Server/server.py --query engineer --limit 5 --no-open
```

One portal:

```powershell
python backend/Server/server.py --source arbeitnow --query automation --limit 5
```

Portals: `greenhouse`, `lever`, `ashby`, `remotive`, `remoteok`, `arbeitnow`, `adzuna`, `linkedin`.

```powershell
python backend/Server/server.py --list
```

`--limit` is the number of jobs per portal. `--query` matches the title, company, or location. `--where` matches a city or location. `--no-open` prints the LinkedIn search link without opening the browser.

Each job is printed as JSON with company, role, experience, skill, salary, added on, location, description, and link. `description` has `about company` and `job description`. With `--source all`, each record also includes `portal`.

## One company or a few companies

Greenhouse, Lever, and Ashby search the company list in `backend/connectors/companies.py`. To search only some of those companies, pass their board tokens with `--boards`:

```powershell
python backend/Server/server.py --source greenhouse --boards discord --query engineer --limit 5
```

```powershell
python backend/Server/server.py --source all --boards discord,cloudflare,spotify --query engineer --limit 5 --no-open
```

`--boards` applies to Greenhouse, Lever, and Ashby. A token that is not on that portal is skipped. Remotive, RemoteOK, and Arbeitnow do not use company tokens.

## Sort

`--sort` accepts `salary`, `date` (added on), and `exp` (experience). Separate keys with commas.

| Command | Order |
|---|---|
| `--sort salary` | Highest pay first |
| `--sort date` | Newest added on first |
| `--sort exp` | Lowest experience first |
| `--sort salary_asc` | Lowest pay first |
| `--sort date_asc` | Oldest added on first |
| `--sort exp_desc` | Highest experience first |
| `--sort salary_asc,date_asc` | Lowest pay first, then oldest date |

Jobs with a blank salary, date, or experience stay at the end.

```powershell
python backend/Server/server.py --source all --query automation --limit 5 --sort salary --no-open
```

```powershell
python backend/Server/server.py --source arbeitnow --query automation --limit 5 --sort exp_desc
```

## Jobs page (web app)

The same jobs can be browsed in the browser. Start the API in one terminal:

```powershell
python backend/Server/server.py
```

That serves `http://127.0.0.1:8001/api/jobs`. A fetch keeps openings from the last 15 days and writes one file per portal under `backend/data/` as results arrive, so the jobs page shows cards while other portals are still loading. The finished list for each user is also saved in Firestore. Refresh starts that fetch again. The jobs page shows 20 jobs per page. Search, portal, date, and sort run in the browser.

Start the web app in a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open [http://localhost:8002/jobs](http://localhost:8002/jobs) and sign in. The page has search, location, and portal, plus posted-date buttons (Today, Last 7 days, Last 30 days) and sorting, which apply instantly to the loaded list. Each card shows the company, role, location, work mode, experience, salary, and an Apply link on the right. Click a card to open the description underneath it.

Point the page at a different API host with `VITE_JOBS_API_URL` in the repository `.env`.

## Adzuna

Adzuna is skipped until both keys are set. Create them at [developer.adzuna.com](https://developer.adzuna.com/).

```powershell
$env:ADZUNA_APP_ID = "your-app-id"
$env:ADZUNA_APP_KEY = "your-app-key"
python backend/Server/server.py --source adzuna --query engineer --where Bengaluru --country in
```
