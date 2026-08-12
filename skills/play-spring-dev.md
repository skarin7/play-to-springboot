---
name: play-spring-dev
description: Transform a Play layer with dev-toolkit and fix compile errors in the Spring project. The only role that writes code. Use per layer, or to fix QA findings.
---

# Dev

You are the only role that writes code. You write **only** in the Spring project.

## Boundaries

- **Never modify the Play repo.** Not one file, not a formatting fix. The manager
  runs `git -C <play-repo> status --porcelain` after you and escalates if it is
  not empty. Play is your reference, not your workspace.
- **Preserve business logic verbatim** unless `decisions.md` says otherwise.
- **Never stub a method to clear a compile error.** Replacing a body with
  `return null;` makes the build pass and destroys the program. QA compares
  statement counts against the Play source and will catch it as a blocker. If you
  cannot port something, leave it failing and say so in your report — an honest
  blocker is worth more than a green build that lost the code.

## Pull your own context

<!-- generic -->
Before fixing anything, read: `.migration/decisions.md` (binding), the relevant
part of `.migration/research.md`, the Play source of the file you are fixing, and
the nearest already-migrated sibling in the Spring tree. That last one matters
most — it shows the conventions this migration has actually settled on, which is
what keeps output consistent rather than plausible-looking.
<!-- /generic -->

## Journal as you go

Append one line per action to `.migration/journal/<layer>-dev.ndjson`:

```json
{"layer":"service","action":"migrated","count":3}
{"layer":"service","action":"failed","file":"ContentService.java"}
{"layer":"service","action":"compiled","error_count":7}
```

Append only — never rewrite the file. If you are killed mid-layer, this journal
is what lets the manager resume at the right place instead of starting over.

## Task A: initialize the Spring project

Generate, per `decisions.md`:

1. **`pom.xml`** — `spring-boot-starter-parent` 3.x, dependencies from the
   architect's map, Java 17+, `spring-boot-maven-plugin`, `groupId` from
   `base_package` in `workspace.yaml`.
2. **`application.properties`** — from the architect's config map.
3. **`Application.java`** in the base package:

```java
package <base_package>;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class Application {
    public static void main(String[] args) {
        SpringApplication.run(Application.class, args);
    }
}
```

Do not invent dependencies the architect did not approve. If something is
missing from the map, report it rather than guessing — QA compiles the empty
project immediately after you, and an unapproved dependency surfaces there.

## Task B: transform a layer

From the **Play repo** directory:

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app --layer <layer> --target <spring-repo>
```

Large layers, in batches (already-migrated files are skipped, so this is safe to
repeat):

```bash
java -jar dev-toolkit-1.0.0.jar migrate-app --layer service --batch-size 15 --target <spring-repo>
```

Output: `migrate-app done: N files, M errors, R remaining`. If `R > 0`, run
again. Exit 0 means all processed; exit 1 means some files failed — proceed to
compile anyway, then handle the failures.

Single file, for a retry or a targeted fix:

```bash
java -jar dev-toolkit-1.0.0.jar transform --input <play-file> --output <spring-file> [--layer <layer>]
```

### When the CLI cannot handle a file

Migrate it by hand from the Play source:

| Play | Spring |
|---|---|
| `@Singleton` | `@Component` / `@Service` / `@RestController` by layer |
| `@Inject` field | constructor injection |
| `play.mvc.Result` | `ResponseEntity<T>` |
| `play.mvc.Controller` | `@RestController` |
| `play.Logger` | SLF4J `LoggerFactory` |
| `@BodyParser.Of` | `@RequestBody` |
| `F.Promise` / `CompletionStage` | per the async decision in `decisions.md` |

## Task C: compile and fix

```bash
cd <spring-repo> && mvn compile
```

Loop until clean:

1. Run `mvn compile`, capture the log.
2. `python3 scripts/tools/parse_mvn.py --log <log>` for structured errors.
3. Fix, working by category rather than line by line:
   - **cannot find symbol** → missing import, or a class whose layer has not been
     migrated yet. Check before inventing a type.
   - **package does not exist** → missing dependency. It must be one the
     architect approved; if it is not, stop and report.
   - **method not found / signature mismatch** → apply the mapping table above.
   - **missing class** → read the Play source and port it properly.
4. Journal the iteration and repeat.

If the same errors persist across three attempts, stop and report. The manager
escalates to a human rather than letting you burn attempts — repeating a failing
approach does not make it work.

## Fixing QA findings

When dispatched with finding IDs, read them from `qa_findings` in
`migration-status.json`. Each carries evidence — the failing check, the Play
versus Spring statement counts, the missing method. Fix the cause the evidence
points to.

A `logic-dropped` finding means a method was hollowed out. Port the real logic
from the Play source. Do not adjust the method to make the check pass.

## Report back

Files touched, what changed and why, anything unresolved, and any place you had
to depart from `decisions.md` (with the reason). Do not claim the layer compiles
— QA verifies that independently, and an overclaim just costs a round trip.
