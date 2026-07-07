# Mystery State — Complete Data Model

This document specifies the full state of a generated mystery: every entity,
every clue type, and how they connect. It is the contract the generator, the
solvability checker, the runtime simulation, and (later) the LLM dialogue
layer all share.

Guiding principle: **the mystery is a simulation artifact, not an authored
object.** Clues are byproducts of events that actually happened in a
simulated past. The state model therefore separates four layers:

| Layer | What it holds | Mutability at game time |
|---|---|---|
| 0. World | Map, locations, time model | Static |
| 1. Cast | Characters, relationships, secrets, motives, schedules | Static (definitions) |
| 2. Ground truth | The event log of the simulated past, incl. the crime | Immutable — this is what happened |
| 3. Evidence | Facts, clues, NPC knowledge states, testimony policies | Semi-static (clues can decay/be destroyed) |
| 4. Runtime | The live five days: player state, killer agency, clue lifecycle | Fully dynamic |

Types below are written in TypeScript-ish pseudocode for precision; the
implementation language is not decided by this document.

---

## Layer 0 — World

### Time

Simulation granularity is the **minute**; presentation granularity is the
**time block** (NPCs think in blocks, forensics in windows).

```ts
type Minute = number;            // minutes since sim start
type TimeWindow = { earliest: Minute; latest: Minute };

type TimeBlock = "dawn" | "morning" | "midday" | "afternoon"
               | "evening" | "night" | "deep_night";
```

Canonical calendar: the sim runs `D-3 .. D0` (three days of prelude, murder
on the night of `D0`), the player plays `D1 .. D5`. The game opens on the
morning of `D1` — "the day after."

### Locations

```ts
type LocationId = string;

interface Location {
  id: LocationId;
  name: string;                       // "The Old Mill"
  kind: "home" | "workplace" | "public" | "outdoor" | "landmark";
  owner?: CharacterId;                // whose home/shop it is
  adjacency: { to: LocationId; travelMinutes: number }[];
  sightlines: LocationId[];           // locations observable from here
  accessibility: "open" | "knock" | "locked" | "hidden";
  containers: ContainerId[];          // searchable spots (drawer, millpond, hearth)
}
```

`sightlines` matter: they are how witness observations get generated
(someone at the bridge can see the mill road). `containers` are where
physical clues live and what "searching a location" enumerates.

---

## Layer 1 — Cast

### Characters

```ts
type CharacterId = string;

interface Character {
  id: CharacterId;
  name: string;
  age: number;
  occupation: string;                 // drives schedule, expertise, plausible props
  home: LocationId;
  workplace?: LocationId;
  personality: PersonalityProfile;    // see below
  expertise: ExpertiseTag[];          // "medicine", "smithing", "hunting", "accounts"...
  schedule: ScheduleEntry[];          // canonical weekly routine
  physical: { build: "slight"|"average"|"strong"; gait?: string; shoeSize: number };
}

interface ScheduleEntry {
  block: TimeBlock;
  location: LocationId;
  activity: string;                   // "tending bar", "sleeping", "walking the dog"
  regularity: number;                 // 0..1 — how reliably they keep this slot
}
```

`expertise` powers expert testimony (the herbalist can date a wound; the
carpenter recognizes tool marks). `physical` powers trace matching
(footprint size, strength needed for the blow). `regularity` powers
gap-detection: a deviation from a high-regularity slot is itself a clue.

`PersonalityProfile` holds the knobs the dialogue layer and lie model need:

```ts
interface PersonalityProfile {
  candor: number;        // 0..1 — baseline willingness to share
  composure: number;     // 0..1 — resistance to pressure when lying
  gossip: number;        // 0..1 — how much secondhand info they absorb/spread
  grudgeAgainst: CharacterId[];  // will volunteer dirt on these people
  voice: string;         // freeform style notes, consumed by the LLM renderer later
}
```

### Relationships

A typed, directed multigraph. Motives are *derived from* this graph, never
invented independently of it.

```ts
type RelationKind =
  | "family" | "marriage" | "romance" | "affair"
  | "friendship" | "rivalry" | "grudge"
  | "debt"            // A owes B (amount in payload)
  | "employment"      // A works for B
  | "blackmail"       // A holds leverage over B (payload: the secret)
  | "inheritance";    // A inherits from B (payload: the asset)

interface Relation {
  from: CharacterId;
  to: CharacterId;
  kind: RelationKind;
  intensity: number;        // 0..1
  publiclyKnown: boolean;   // secret relations feed the Secret system
  payload?: Record<string, unknown>;
}
```

### Secrets

Secrets are the engine of both the cover-up and the red herrings. Every lie
in the game traces back to exactly one secret.

```ts
type SecretId = string;

interface Secret {
  id: SecretId;
  holder: CharacterId;
  kind: "murder" | "affair" | "theft" | "poaching" | "debt_shame"
      | "smuggling" | "hidden_identity" | "witnessed_something";
  protectedFacts: FactId[];       // ground-truth facts this secret hides
  coverStory: Claim[];            // the false claims told instead (see Testimony)
  confidants: CharacterId[];      // who else knows (they may lie too, or crack)
  breaksUnder: BreakCondition[];  // what makes the holder come clean
  exculpatory: boolean;           // true for red herrings: resolving it CLEARS the holder
}

type BreakCondition =
  | { type: "evidence_presented"; clue: ClueId }
  | { type: "contradiction_shown"; facts: [FactId, FactId] }
  | { type: "confidant_broke"; secret: SecretId }
  | { type: "trust_at_least"; value: number };
```

`exculpatory` is the key red-herring mechanic: an innocent's secret, once
cracked, *positively rules them out* (the poacher wasn't at the mill — he
was in the north woods, and the snares prove it). Suspicion resolves into
exoneration, which feels like progress rather than a dead end.

### Motives

```ts
interface Motive {
  suspect: CharacterId;
  victim: CharacterId;
  kind: "greed" | "revenge" | "jealousy" | "silencing" | "fear" | "inheritance";
  derivedFrom: Relation[];   // the relationship edges that justify it
  strength: number;          // 0..1 — used by generator to pick killer & herrings
  originEvents: EventId[];   // ground-truth events where this motive was born/escalated
}
```

The generated case keeps **3–5 characters with nonzero motive**. One is the
killer; the rest are the viable-suspect pool the solver must be able to
eliminate.

---

## Layer 2 — Ground Truth

### The event log

The prelude simulation (`D-3 .. D1` morning) emits an append-only log of
atomic events. This log **is** the truth; everything else derives from it.

```ts
type EventId = string;

interface Event {
  id: EventId;
  time: Minute;
  location: LocationId;
  actor: CharacterId;
  action: Action;                  // typed verb, below
  observedBy: Observation[];       // computed from sightlines + presence
  emits: TraceId[];                // physical traces created (possibly none)
  partOf?: CrimeStage;             // tagged if it belongs to the crime chain
}

type Action =
  | { verb: "move"; from: LocationId; to: LocationId }
  | { verb: "converse"; with: CharacterId; topic: string; overheardRisk: number }
  | { verb: "take" | "drop" | "hide" | "destroy"; object: ObjectId; container?: ContainerId }
  | { verb: "transact"; with: CharacterId; what: string; recordedIn?: ObjectId } // ledger!
  | { verb: "work" | "sleep" | "eat" | "idle"; detail: string }
  | { verb: "attack"; target: CharacterId; weapon: ObjectId; outcome: "kill"|"injure" }
  | { verb: "tamper"; target: TraceId | ObjectId };  // cover-up & runtime killer agency

interface Observation {
  observer: CharacterId;
  fidelity: "clear" | "partial" | "glimpse";  // glimpse: "a tall figure", not a name
  at: Minute;
}
```

`fidelity` is what makes eyewitness testimony a puzzle instead of an answer
key: a `glimpse` observation generates a fact like *"someone in a heavy
cloak crossed the bridge around eleven"* — evidence that narrows, but does
not name.

### The crime record

One structured object indexes the murder's full causal chain into the log:

```ts
interface CrimeRecord {
  victim: CharacterId;
  killer: CharacterId;
  motive: Motive;
  method: {
    weapon: ObjectId;
    weaponClass: "blade" | "blunt" | "poison" | "strangulation" | "fall";
    requiredTraits?: Partial<Character["physical"]>; // e.g. strength for the blow
  };
  timeOfDeath: Minute;               // exact truth
  scene: LocationId;
  stages: Record<CrimeStage, EventId[]>;
}

type CrimeStage =
  | "motive_origin"     // the debt incurred, the affair discovered...
  | "trigger"           // the final straw, usually D-1 or D0
  | "preparation"       // acquiring weapon, learning victim's routine
  | "execution"         // the murder itself
  | "coverup_immediate" // night of: cleaning, hiding weapon, sneaking home
  | "coverup_ongoing";  // D1..D5 runtime actions (see Layer 4)
```

Every stage **must emit at least one trace or observation** — the generator
enforces this. A crime with an evidence-free stage is rejected, because
that stage would be undiscoverable and the reconstructed story would have a
hole.

### The body

The victim's body is the case's anchor clue and gets first-class treatment:

```ts
interface BodyReport {
  foundAt: LocationId;
  foundBy: CharacterId;              // the discovery is itself an event with a witness
  woundProfile: {
    class: CrimeRecord["method"]["weaponClass"];
    detail: string;                  // "single blow, left temple, struck from behind"
    impliesWeaponShape?: string;     // matchable against candidate objects
    impliesAttacker?: string;        // "considerable force" | "shorter than victim"...
  };
  timeOfDeathWindow: TimeWindow;     // what forensics can honestly say (± hours)
  sceneState: TraceId[];             // traces present at the scene on D1 morning
  movedPostMortem: boolean;
}
```

Note the window: the game never hands the player the exact `timeOfDeath`.
Narrowing the window (last sighting alive, the lamp that went out, the dog
that barked) is core gameplay.

---

## Layer 3 — Evidence

This layer is the heart of the model. Three distinct concepts, kept strictly
separate:

- **Fact** — an atomic true proposition about the ground truth.
- **Clue** — a discoverable *carrier* that evidences one or more facts.
- **Belief** — what some NPC thinks is true (possibly wrong).

One fact may be carried by several clues (redundancy). One clue may bear on
several facts. NPCs testify from beliefs, not facts — that gap is where
lies and honest mistakes live.

### Facts

```ts
type FactId = string;

interface Fact {
  id: FactId;
  proposition: Proposition;          // structured, so the solver can reason over it
  tier: "critical" | "supporting" | "exculpatory" | "flavor";
  derivedFromEvents: EventId[];
}

type Proposition =
  | { p: "was_at";        who: CharacterId; where: LocationId; when: TimeWindow }
  | { p: "was_not_at";    who: CharacterId; where: LocationId; when: TimeWindow }
  | { p: "owns" | "possessed"; who: CharacterId; object: ObjectId; when?: TimeWindow }
  | { p: "did";           event: EventId }
  | { p: "relation";      relation: Relation }
  | { p: "motive";        motive: Motive }
  | { p: "weapon_is";     object: ObjectId }
  | { p: "died_between";  window: TimeWindow };
```

Tiers:

- **critical** — needed for some pillar of the solution (see Layer 5's
  accusation model). The solver's redundancy guarantee applies to these.
- **supporting** — narrows or corroborates; makes the case easier, not possible.
- **exculpatory** — clears a non-killer suspect (ties to `Secret.exculpatory`).
- **flavor** — world texture; also the raw material for organic red herrings.

### Clue taxonomy

Every clue has one `kind` from this closed taxonomy. The taxonomy is the
generator's checklist — a good case draws from most rows.

**Physical clues** (live at locations/containers, subject to decay):

| Kind | Examples | Notes |
|---|---|---|
| `trace_mark` | footprints, blood spatter, mud on a floor, scratches on a lock, trampled grass | Matchable to `Character.physical`; most decay-prone |
| `object_present` | the weapon, a dropped button, a snagged cloth scrap, a pipe left behind | Ownership matters: `possessed` facts link objects to people |
| `object_absent` | the missing mallet from a rack of tools, the victim's absent strongbox key | **Negative clues** — absence as evidence; discovered by knowing what *should* be there |
| `object_state` | a lamp burned dry (lit all night), a hearth cold by dawn, a door forced vs. unlocked | Encodes timing and access information |
| `document` | ledger entries, letters, a will, an IOU, a diary, church records | Durable, high-reliability; often carries `relation`/`motive` facts |
| `body_forensic` | wound shape, time-of-death window, matter under fingernails | Sourced from `BodyReport`; some require the expert NPC to read |

**Testimonial clues** (live in NPC knowledge states, delivered via dialogue):

| Kind | Examples | Notes |
|---|---|---|
| `sighting` | "I saw Tobin on the mill road near eleven" | Fidelity-limited per the `Observation` |
| `hearsay` | "Marta says the miller and his brother quarreled" | Secondhand; reliability discounted; may mutate in transmission |
| `background` | who owes whom, who loves whom, old grudges, the victim's habits | The gossip network; powers triage/pointers |
| `alibi_claim` | "I was home asleep, ask my wife" | A *claim*, not a fact — the thing contradictions attack |
| `behavioral` | "He's washed that coat twice this week"; "she hasn't opened the shop since" | Generated from schedule deviations post-`D0` |
| `expert_reading` | the herbalist dates the wound; the carpenter IDs the tool marks | Requires bringing evidence to the right `expertise` |

**Derived clues** (not stored — *computed* in the player's journal when
their ingredients are both known):

| Kind | Definition |
|---|---|
| `contradiction` | Two known claims/facts that cannot both hold ("home by nine" vs. "his light was on at midnight") |
| `corroboration` | Independent clues agreeing — raises confidence in a disputed claim |
| `gap` | A person unaccounted-for during a window their `regularity` says they should be placed |

### The clue record

```ts
type ClueId = string;

interface Clue {
  id: ClueId;
  kind: ClueKind;                          // one of the taxonomy rows above
  evidences: { fact: FactId; strength: "proves" | "suggests" }[];
  misleads?: { toward: CharacterId; dispelledBy: FactId[] };  // honest red-herring wiring
  carrier: Carrier;
  discovery: DiscoveryRequirement[];       // ALL must hold (OR = separate clue entries)
  lifecycle: ClueLifecycle;
  reliability: number;                     // 0..1 — hearsay < sighting < document
}

type Carrier =
  | { type: "location"; where: LocationId; container?: ContainerId }
  | { type: "npc";      who: CharacterId }                    // testimonial
  | { type: "object";   object: ObjectId }                    // readable/examinable
  | { type: "body" };

type DiscoveryRequirement =
  | { type: "search";  where: LocationId; container?: ContainerId }
  | { type: "examine"; object: ObjectId }
  | { type: "topic_known"; anyOf: FactId[] }   // player must know enough to ask
  | { type: "present_evidence"; clue: ClueId; to: CharacterId }
  | { type: "trust_at_least"; with: CharacterId; value: number }
  | { type: "expertise"; tag: ExpertiseTag }   // needs the right NPC's help
  | { type: "time_window"; window: TimeWindow };  // only findable while it exists
```

`misleads` is how red herrings stay *fair*: a misleading clue genuinely
points at an innocent, but the model records which facts dispel it, and the
solver verifies those facts are reachable. No unfalsifiable frame-ups.

### Clue lifecycle

```ts
interface ClueLifecycle {
  state: "latent" | "discoverable" | "discovered" | "degraded" | "destroyed";
  decay?: { at: Minute; to: "degraded" | "destroyed"; cause: "weather" | "traffic" | "routine" };
  threatenedBy?: CharacterId;   // the killer plans to destroy this (runtime agency)
}
```

Decay is scheduled at generation time (rain on `D2` night degrades outdoor
`trace_mark`s; the millpond gets dredged for the fair on `D4`). This is
what gives the five days their texture: the crime scene is a melting asset.

### NPC knowledge states & testimony

```ts
interface Belief {
  holder: CharacterId;
  about: FactId | Proposition;     // can believe things that aren't facts (mistakes)
  accuracy: "true" | "mistaken" | "vague";
  source: { type: "witnessed"; event: EventId }
        | { type: "told_by"; who: CharacterId; at: Minute }
        | { type: "inferred" };
  learnedAt: Minute;
  willShare: SharePolicy;
}

type SharePolicy =
  | { mode: "freely" }                                   // volunteers if topic arises
  | { mode: "if_asked" }
  | { mode: "reluctant"; needs: BreakCondition[] }       // trust/pressure gated
  | { mode: "lie"; secret: SecretId; claim: Claim }      // tells the cover story instead
  | { mode: "withhold"; secret: SecretId };              // omits, deflects

interface Claim {                   // a proposition someone asserts (maybe falsely)
  speaker: CharacterId;
  proposition: Proposition;
  truth: boolean;                   // ground-truth flag, hidden from player
  coversSecret?: SecretId;
}
```

Rules the generator enforces:

1. **Every lie belongs to a secret.** No freelance lying — it would make
   contradictions meaningless noise.
2. **Every belief has provenance.** An NPC knows a thing because they
   witnessed an event or someone told them (and *that* telling is itself an
   event). Hearsay chains are real chains the player can walk back.
3. **Gossip propagation runs in the prelude sim** and continues at runtime:
   high-`gossip` NPCs absorb and re-emit `background` and `hearsay`
   beliefs, sometimes with `accuracy: "mistaken"` mutations.

This structure is exactly the grounding package the LLM dialogue layer will
consume per-conversation: *character profile + their beliefs + share
policies + active claims*. The LLM renders voice; this layer decides truth.

---

## Layer 4 — Runtime State

The live game state across `D1 .. D5`.

```ts
interface RuntimeState {
  clock: Minute;
  deadline: Minute;                       // end of D5
  npcPositions: Record<CharacterId, LocationId>;   // schedule-driven, perturbable
  clueStates: Record<ClueId, ClueLifecycle>;
  killerAgenda: KillerAction[];           // see below
  gossipQueue: PendingTransmission[];     // beliefs still propagating
  suspicionOfPlayer: Record<CharacterId, number>;  // NPCs react to being pressed
  player: PlayerState;
}
```

### Killer agency

The killer is a live agent with a small planner. At generation time they get
an *agenda* of contingent actions; at runtime these fire on triggers:

```ts
interface KillerAction {
  trigger: { type: "scheduled"; at: Minute }
         | { type: "player_found"; clue: ClueId }
         | { type: "npc_told_player"; fact: FactId }
         | { type: "accused_wrongly" };
  action: { type: "destroy_clue"; clue: ClueId }        // must be reachable & plausible
        | { type: "plant_clue";  clue: ClueId }          // frames a herring further
        | { type: "spread_rumor"; claim: Claim; via: CharacterId }
        | { type: "flee" }                               // late-game fail-forward
        | { type: "slip_up"; emits: ClueId };            // nervous mistakes CREATE clues
  visibility: number;   // chance the action itself is observed → new Observation
}
```

Two invariants keep this fair: **(1)** killer actions can degrade the case
but never below the solver's guaranteed floor (a protected core of clues is
marked untouchable at generation), and **(2)** every destructive action has
`visibility > 0` — covering up is itself risky and can hand the player the
thread. Pressure on the killer should *generate* evidence, not only erase it.

### Player state

```ts
interface PlayerState {
  location: LocationId;
  journal: {
    knownFacts: FactId[];
    heardClaims: Claim[];                  // testimony collected, true or not
    contradictions: [Claim | FactId, Claim | FactId][];  // auto-derived
    markedSuspects: CharacterId[];
  };
  evidenceInventory: ClueId[];             // portable physical/document clues
  trust: Record<CharacterId, number>;
  accusationsMade: number;                 // accusations are scarce (1, maybe 2)
}
```

Time costs are the economy: travel per the adjacency graph, conversations
~20–40 sim-minutes, searches ~30–60, sleep mandatory. A rough budget of
**~35–45 meaningful actions** across five days, against a world holding
roughly twice that much discoverable material — the surplus is what forces
triage.

---

## Layer 5 — Solution & Solvability

### The accusation model

To win, the player accuses a character and must back three pillars with
evidence from their journal:

```ts
interface Accusation {
  accused: CharacterId;
  pillars: {
    opportunity: ClueId[];   // places accused at scene in the ToD window / breaks alibi
    means: ClueId[];         // links accused to the weapon class/object
    motive: ClueId[];        // establishes the motive facts
  };
}
```

Scoring: each pillar is satisfied if its cited clues collectively `prove`
(or multiply-`suggest`) the pillar's critical facts. Correct accusation with
all pillars → full ending. Correct with weak pillars → partial ending (he
walks, or confesses only if `composure` is low). Wrong accusation →
consequences (the real killer's `accused_wrongly` trigger fires; one
accusation is spent). This is what makes evidence *matter* beyond intuition.

### The solvability certificate

Generation is not complete until the solver emits a certificate:

```ts
interface SolvabilityCertificate {
  uniqueness: {                       // for every non-killer with a motive:
    suspect: CharacterId;
    eliminatedBy: FactId[];           // reachable exculpatory facts
  }[];
  pillarPaths: {                      // for every critical fact:
    fact: FactId;
    independentPaths: DiscoveryPath[];  // MUST be ≥ 2 (target 3)
  }[];
  budgetFeasible: {
    minActionsToSolve: number;        // solver's cheapest full solution
    budget: number;                   // must satisfy min ≤ 0.6 × budget
  };
  decaySafe: boolean;                 // solvable even if all decayable clues expire
                                      // AND the killer executes their full agenda
  herringsFair: boolean;              // every `misleads` has reachable dispelling facts
}

type DiscoveryPath = { steps: DiscoveryRequirement[]; costMinutes: number };
```

Checks, in order:

1. **Reconstruction** — from the full discoverable clue set, the crime chain
   (all six stages) is derivable. No evidence-free stages.
2. **Uniqueness** — killer is the only character satisfying all three pillars
   once exculpatory facts are found. Every herring is *positively* clearable.
3. **Redundancy** — rule of three (floor of two) on every critical fact,
   via *independent* paths (no shared single point of failure — e.g. not
   all through one NPC who might stonewall).
4. **Budget feasibility** — a competent-but-not-omniscient path to the
   solution fits in ~60% of the action budget, leaving room for dead ends.
5. **Decay & agency safety** — run the checks again against the worst-case
   timeline (all decay fires, killer agenda fully executes, minus the
   protected core). Still solvable ⇒ certificate granted; else regenerate
   or patch (inject a disambiguating trace, protect a clue, soften decay).

A case ships only with a certificate. This is the load-bearing quality gate.

### Interestingness heuristics (soft checks)

Solvable ≠ good. After the hard gate, score and threshold on:

- ≥ 1 **inferential leap**: a critical fact whose cheapest path is a
  derived clue (contradiction or gap), not a direct find.
- ≥ 2 exculpatory secrets that *feel* guilty before they're cracked.
- Clue-kind diversity: critical facts spread across ≥ 4 taxonomy rows
  (a case solved entirely by documents is an audit, not a mystery).
- The killer's cover story survives casual questioning — it breaks only
  under evidence, never under "asking twice."

---

## Worked micro-example (6 characters, abridged)

**Ground truth:** Aldric the miller (also the village moneylender) is found
dead in the mill on `D1` morning, skull struck from behind — one blow,
considerable force. Killer: **Tobin the carpenter**, whose workshop Aldric
was about to seize over a defaulted loan (`motive: fear/greed`,
`origin: D-30 debt`, `trigger: D0 seizure notice`, recorded in Aldric's
ledger). Method: Tobin's own long-handled mallet; after the murder he threw
it in the millpond and walked home by the back path.

**Suspect pool** (nonzero motive): Tobin; **Edwin**, the victim's brother,
who inherits the mill; **Ysolt**, whom Aldric was blackmailing over an
affair.

**Sample of the clue set:**

| Clue | Kind | Evidences (tier) |
|---|---|---|
| Wound shape "broad, rounded, wooden" | `body_forensic` | weapon_is: mallet-class (critical/means) |
| Empty peg on Tobin's tool rack | `object_absent` | Tobin possessed a long mallet (critical/means) |
| Mallet in the millpond — **dredged for the fair on D4**, destroyed after | `object_present`, decaying | weapon_is + possessed (proves both) |
| Aldric's ledger: Tobin's default, seizure dated `D1` | `document` | motive (critical) |
| Widow Bren, glimpse: "a tall figure on the back path, near midnight" | `sighting` (glimpse) | was_at: someone, back path, ToD window (supporting) |
| Tobin's claim: "home asleep by ten, ask Sela" | `alibi_claim` (lie) | — covers `murder` secret |
| Sela's hesitation; breaks on contradiction | `reluctant` testimony | was_not_at: Tobin, home, 22:00–01:00 (critical/opportunity) |
| Edwin's claim: "at the tavern till close" (lie) | `alibi_claim` | covers `poaching` secret (**exculpatory**: snares in the north woods clear him) |
| Ysolt's burned letters in her hearth | `trace_mark`, misleads → Ysolt | dispelledBy: blackmail payment in ledger dated `D0` — she'd already paid |
| Herbalist reads the body: died 23:00–01:00 | `expert_reading` | died_between (critical) |

**Certificate sketch:** means has three paths (wound→expert reading, empty
peg, pond mallet — pond decays `D4`, others survive); opportunity has two
(Sela cracks; Bren's glimpse + Tobin's shoe size matching back-path prints,
prints degrade in `D2` rain — hence Sela is in the protected core);
uniqueness holds (Edwin cleared by poaching evidence, Ysolt by the payment
record). One planned inferential leap: nobody *tells* you Tobin was out —
you prove his alibi false and the gap does the accusing.

---

## Generation pipeline (build order)

1. **World gen** — map, locations, sightlines, containers.
2. **Cast gen** — characters, schedules, relationship graph.
3. **Tension pass** — derive motives; pick victim (max inbound motive) and
   killer (strong motive + feasible opportunity); pick 2–3 herring secrets.
4. **Prelude simulation** — `D-3..D0`: routine life + motive origin/trigger
   events + gossip propagation; then the crime chain; then immediate cover-up.
   Emit event log, traces, observations, beliefs.
5. **Evidence derivation** — facts from events; clues from traces,
   observations, documents, body; tier assignment; lie/cover-story wiring.
6. **Decay & agenda scheduling** — weather, routines, killer agenda,
   protected core selection.
7. **Solve & certify** — the Layer 5 checks; patch or regenerate on failure.
8. **Interestingness scoring** — soft heuristics; regenerate below threshold.

Steps 1–8 run headless and dump: ground-truth timeline, full clue table,
certificate, and a prose "case summary" for human review. That dump is the
first milestone — ten generated cases we can read and judge.
