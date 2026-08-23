#!/usr/bin/env bb
;; Downloads the JLCPCB parts database and builds a self-contained `components` table,
;; plus normalized numeric spec tables (`resistors`, `capacitors`, `inductors`) for parametric queries.
;; I browse this with https://sqlitebrowser.org/ and also have LLM agents use it to find potential parts for me.
;;
;; Usage: download-jlcpcb-parts-database [output-file]
;;
;; The source is the jlcparts project (https://github.com/yaqwsx/jlcparts), which publishes
;; its ~5.7 GB sqlite database daily as a split zip archive (cache.z01, cache.z02, ..., cache.zip)
;; because GitHub Pages cannot serve single files that large.
;;
;; Output defaults to ./jlcpcb_parts.db.
;; The download and the build happen in a scratch directory under ${XDG_CACHE_HOME:-~/.cache};
;; an existing output file is replaced only after the new database is complete.
;; When run interactively, the script first reports the download size, scratch location,
;; and space needed, and asks for confirmation.
;; The build date lives in a `meta` table inside the DB rather than in the filename or
;; filesystem mtime, so it survives copying/rsync/git.
;; Query it with:
;;   SELECT value FROM meta WHERE key='generated_at';
;;
;; Design: Clojure generates the SQL strings; the sqlite3 CLI does the bulk work on stdin.
;; The 7M source rows never stream through Clojure.

(require '[babashka.fs :as fs]
         '[babashka.http-client :as http]
         '[babashka.process :as p]
         '[clojure.java.io :as io]
         '[clojure.string :as str])

(def base-url "https://yaqwsx.github.io/jlcparts/data")

;; Single source of truth for unit parsing in the generated SQL.
;; Suffixes are canonical upstream (checked against the live data).
;; Matching must stay case-sensitive because of mΩ vs MΩ, so the generator
;; emits substr equality, never LIKE (SQLite LIKE folds ASCII case).
(def unit-scales
  {:ohms    {"mΩ" 1e-3 "Ω" 1.0 "kΩ" 1e3 "MΩ" 1e6 "GΩ" 1e9}
   ;; DCR special case: upstream writes "8MΩ" on power inductors where it means mΩ
   ;; (a multi-amp inductor cannot have megaohm winding resistance), so MΩ reads as
   ;; milliohms here.
   :dcr-ohms {"mΩ" 1e-3 "MΩ" 1e-3 "Ω" 1.0 "kΩ" 1e3}
   :farads  {"pF" 1e-12 "nF" 1e-9 "uF" 1e-6 "µF" 1e-6 "mF" 1e-3 "F" 1.0}
   :henries {"nH" 1e-9 "uH" 1e-6 "µH" 1e-6 "mH" 1e-3 "H" 1.0}
   :watts   {"mW" 1e-3 "kW" 1e3 "W" 1.0}
   :volts   {"kV" 1e3 "mV" 1e-3 "V" 1.0}
   :amps    {"mA" 1e-3 "uA" 1e-6 "A" 1.0}})

(declare build-sql components-sql meta-sql family-specs side-table-sql
         value-case-sql fraction-when-sql tolerance-sql tempco-sql dielectric-sql
         canon-sql sql-quote plan-summary now-iso
         discover-parts! head-size! existing-db-meta! confirm! tty?
         download! extract! sqlite-file? build! install! delete-with-siblings!
         du-h! die!)

;; ---- entry point ----

(defn -main [& args]
  (let [output (or (first args) "jlcpcb_parts.db")
        generated-at (now-iso)]
    (when-not (fs/which "unzip")
      (die! "this script needs 'unzip' to extract the split archive"))
    (when-let [dir (fs/parent output)]
      (fs/create-dirs dir))
    ;; Scratch space goes under the user cache dir rather than /tmp, which is often a
    ;; small tmpfs that cannot hold the multi-GB extracted database.
    (let [scratch-parent (or (System/getenv "XDG_CACHE_HOME")
                             (str (fs/path (System/getProperty "user.home") ".cache")))
          _ (fs/create-dirs scratch-parent)
          work-dir (fs/create-temp-dir {:dir scratch-parent :prefix "kkh-jlcpcb-build-"})
          stage-file (str output ".new")]
      ;; A failed or aborted run must never touch the existing output database.
      ;; The shutdown hook cleans scratch and stage on all exits, like a bash EXIT trap.
      (.addShutdownHook (Runtime/getRuntime)
                        (Thread. (fn []
                                   (when (fs/exists? work-dir)
                                     (fs/delete-tree work-dir))
                                   (fs/delete-if-exists stage-file))))
      (let [{:keys [part-names total-bytes]} (discover-parts!)
            existing (existing-db-meta! output)]
        (doseq [line (plan-summary {:part-names part-names
                                    :total-bytes total-bytes
                                    :work-dir work-dir
                                    :output output
                                    :existing existing})]
          (println line))
        (confirm!)
        (download! part-names work-dir)
        (let [raw-file (extract! part-names work-dir)
              new-file (fs/path work-dir "new.db")
              row-count (build! raw-file new-file generated-at output)]
          (install! new-file output)
          (println (str "Done: " output " (" (du-h! output) "), "
                        row-count " components, generated_at=" generated-at)))))))

;; ---- pure core: SQL generation ----

(defn build-sql
  "The whole transform as one SQL string for the sqlite3 CLI on stdin."
  [{:keys [new-file generated-at]}]
  (str "ATTACH DATABASE '" (sql-quote (str new-file)) "' AS out;\n\n"
       (components-sql generated-at)
       "\n"
       (str/join "\n\n" (mapcat side-table-sql (family-specs)))
       "\n\n"
       (meta-sql generated-at)
       "\n\nANALYZE out;\n"))

(defn components-sql [generated-at]
  (str "
-- Explicit column types so consumers get a real REAL price column (and typed
-- integer columns) — no CAST(price AS REAL) needed at query time.
CREATE TABLE out.components (
    lcsc          INTEGER PRIMARY KEY,
    mpn           TEXT,      -- manufacturer part number, e.g. 'STM32F103C8T6'
    extended      INTEGER,   -- 0 = basic/preferred (fee-free), 1 = extended
    stock         INTEGER,
    price         REAL,      -- USD, first (smallest-qty / highest) tier; NULL if unknown
    category      TEXT,      -- can be '' (blank upstream) for ~18% of parts
    subcategory   TEXT,
    package       TEXT,
    joints        INTEGER,
    description   TEXT,
    datasheet     TEXT,
    manufacturer  TEXT,      -- manufacturer NAME, e.g. 'Texas Instruments'
    last_on_stock TEXT,      -- ISO date stock was last nonzero; NULL for parts in
                             -- stock now. Set only on the kept zero-stock parts,
                             -- which are usually still orderable as JLCPCB
                             -- \"Global Sourcing\" preorders.
    attributes    TEXT       -- JLCPCB structured specs as a JSON object of
                             -- canonicalized strings, e.g. {\"Resistance\":\"10kΩ\",
                             -- \"Tolerance\":\"±1%\"}; query with json_extract().
                             -- NULL when upstream has none (~6% of parts).
);

INSERT INTO out.components
SELECT lcsc,
       mfr AS mpn,
       -- 1 = extended part (incurs per-BOM-line feeding fee at assembly);
       -- 0 = basic/preferred, i.e. effectively fee-free. JLCPCB's basic and
       -- preferred tiers are functionally the same for our purposes, so we
       -- collapse both into a single \"extended\" boolean.
       CASE WHEN library_type = 'base' OR preferred = 1 THEN 0 ELSE 1 END AS extended,
       stock,
       -- price is an upstream string of quantity tiers like
       -- \"1-199:0.0189,200-599:0.0163,...\": qty range, colon, unit price.
       -- first_tier is the first (smallest-quantity) tier, i.e. the highest
       -- unit price you'd pay; a missing/malformed tier yields NULL (not a
       -- bogus 0.0), and the REAL column affinity above cements the value
       -- as a real number.
       CASE WHEN first_tier LIKE '%:%'
            THEN ROUND(CAST(substr(first_tier, instr(first_tier, ':') + 1) AS REAL), 3)
            ELSE NULL END AS price,
       category, subcategory, package, joints, description, datasheet, manufacturer,
       CASE WHEN stock = 0 THEN date(last_on_stock, 'unixepoch') END AS last_on_stock,
       -- Empty attribute objects become NULL so absence is queryable
       -- (attributes IS NULL) and costs no storage.
       NULLIF(NULLIF(attributes, ''), '{}')
FROM (
    SELECT lcsc, library_type, preferred, stock, last_on_stock, mfr, manufacturer,
           category, subcategory, package, joints, description, datasheet, attributes,
           CASE WHEN instr(price, ',') > 0
                THEN substr(price, 1, instr(price, ',') - 1)
                ELSE price END AS first_tier
    FROM jlc_components
    -- Parts currently in the JLCPCB catalog that are in stock now, or were in
    -- stock within the year before this build. The upstream stock figure is
    -- JLCPCB's own SMT warehouse; recently-stocked parts still carry prices and
    -- are usually preorderable via Global Sourcing, so they stay queryable here.
    WHERE present = 1
      AND (stock > 0
           OR last_on_stock > CAST(strftime('%s', '" (sql-quote generated-at) "', '-365 days') AS INTEGER))
);

-- Indexes for the columns you actually filter / sort on.
CREATE INDEX out.idx_cs_category ON components (category, subcategory);
CREATE INDEX out.idx_cs_price    ON components (price);
CREATE INDEX out.idx_cs_stock    ON components (stock);
CREATE INDEX out.idx_cs_extended ON components (extended);
CREATE INDEX out.idx_cs_package  ON components (package);
CREATE INDEX out.idx_cs_mpn      ON components (mpn);
CREATE INDEX out.idx_cs_manufacturer ON components (manufacturer);

-- Normalized numeric spec tables for the passive families, so parametric
-- queries work without JSON or unit-string parsing: \"100nF\" and \"0.1µF\"
-- both land as farads = 1.0e-7, and range filters like
-- \"WHERE ohms BETWEEN 9e3 AND 11e3\" just work.
--
-- Membership: the part's category names the family, or the category is blank
-- (as it is for ~18% of parts) and the description names it.
-- Tolerance keeps only the symmetric \"±X%\" form; asymmetric ranges like
-- \"-20%~+80%\" become NULL (the raw string remains in description/attributes).
--
-- Every parsed value is canonicalized through printf('%.6e') so that
-- decimal-equal values land on the identical double no matter which unit
-- they were written in: upstream has both \"100nF\" and \"100000pF\", and
-- 100*1e-9 != 100000*1e-12 in floating point, but both canonicalize to
-- the same double as the query literal 100e-9 — so plain = works.
"))

(defn meta-sql [generated-at]
  (str "-- Build provenance: durable, travels with the DB.\n"
       "CREATE TABLE out.meta (key TEXT PRIMARY KEY, value TEXT);\n"
       "INSERT INTO out.meta (key, value) VALUES\n"
       "  ('generated_at', '" (sql-quote generated-at) "'),\n"
       "  ('source_url',   '" base-url "/cache.zip'),\n"
       "  ('row_count',    (SELECT CAST(COUNT(*) AS TEXT) FROM out.components));"))

(defn family-specs
  "One spec per passive family: membership predicate, source attributes,
  output columns with their parse expressions, and the primary-value guard."
  []
  [{:table "resistors"
    :member (str "category = 'Resistors'\n"
                 "       OR (category = '' AND description LIKE '%Resistor%')")
    :attrs [["r" "$.Resistance"]
            ["tol" "$.Tolerance"]
            ["pw" "$.\"Power(Watts)\""]
            ["vv" "$.\"Voltage-Supply(Max)\""]
            ["tc" "$.\"Temperature Coefficient\""]]
    :guard ["r" "Ω"]
    :columns [{:col "ohms" :decl "REAL NOT NULL" :doc "nominal resistance"
               :expr (value-case-sql "r" (:ohms unit-scales))}
              {:col "tolerance_pct" :decl "REAL" :doc "\"±1%\" -> 1.0"
               :expr (tolerance-sql "tol")}
              {:col "power_w" :decl "REAL" :doc "\"62.5mW\" -> 0.0625, \"1/4W\" -> 0.25"
               :expr (value-case-sql "pw" (:watts unit-scales) (fraction-when-sql "pw"))}
              {:col "voltage_v" :decl "REAL" :doc "max working voltage"
               :expr (value-case-sql "vv" (:volts unit-scales))}
              {:col "tempco_ppm" :decl "REAL" :doc "\"±100ppm/℃\" -> 100.0"
               :expr (tempco-sql "tc")}]}
   {:table "capacitors"
    :member (str "category LIKE 'Capacitors%'\n"
                 "       OR (category = '' AND description LIKE '%Capacitor%')")
    :attrs [["c" "$.Capacitance"]
            ["tol" "$.Tolerance"]
            ["vv" "$.\"Voltage Rating\""]
            ["tc" "$.\"Temperature Coefficient\""]]
    :guard ["c" "F"]
    :columns [{:col "farads" :decl "REAL NOT NULL" :doc "nominal capacitance"
               :expr (value-case-sql "c" (:farads unit-scales))}
              {:col "tolerance_pct" :decl "REAL"
               :expr (tolerance-sql "tol")}
              {:col "voltage_v" :decl "REAL" :doc "rated voltage"
               :expr (value-case-sql "vv" (:volts unit-scales))}
              {:col "dielectric" :decl "TEXT" :doc "X7R, C0G, X5R, ...; NULL for non-ceramics"
               :text? true
               :expr (dielectric-sql "tc")}]}
   {:table "inductors"
    :member (str "category LIKE 'Inductors%'\n"
                 "       OR (category = '' AND description LIKE '%Inductor%')")
    :attrs [["l" "$.Inductance"]
            ["tol" "$.Tolerance"]
            ["cur" "$.\"Current Rating\""]
            ["dcr" "$.\"DC Resistance(DCR)\""]]
    :guard ["l" "H"]
    :columns [{:col "henries" :decl "REAL NOT NULL" :doc "nominal inductance"
               :expr (value-case-sql "l" (:henries unit-scales))}
              {:col "tolerance_pct" :decl "REAL"
               :expr (tolerance-sql "tol")}
              {:col "current_a" :decl "REAL" :doc "rated current"
               :expr (value-case-sql "cur" (:amps unit-scales))}
              {:col "dcr_ohms" :decl "REAL" :doc "DC resistance"
               :expr (value-case-sql "dcr" (:dcr-ohms unit-scales))}]}])

(defn side-table-sql
  "-> [create-sql insert-sql index-sql] for one family spec.
  The insert nests three selects: extract attributes, parse units, canonicalize."
  [{:keys [table member attrs guard columns]}]
  (let [[guard-col guard-suffix] guard
        value-col (:col (first columns))
        col-lines (cons {:head "    lcsc          INTEGER PRIMARY KEY"}
                        (for [{:keys [col decl doc]} columns]
                          {:head (str "    " (format "%-13s" col) " " decl) :doc doc}))
        ;; The comma must come before the doc comment or the SQL line comment swallows it.
        create (str "CREATE TABLE out." table " (\n"
                    (str/join "\n"
                              (map-indexed (fn [i {:keys [head doc]}]
                                             (str head
                                                  (when (< (inc i) (count col-lines)) ",")
                                                  (when doc (str "  -- " doc))))
                                           col-lines))
                    "\n);")
        insert (str "INSERT INTO out." table "\n"
                    "SELECT lcsc,\n       "
                    (str/join ",\n       "
                              (for [{:keys [col text?]} columns]
                                (if text? col (canon-sql col))))
                    "\nFROM (\n"
                    "SELECT lcsc,\n       "
                    (str/join ",\n       "
                              (for [{:keys [col expr]} columns]
                                (str expr " AS " col)))
                    "\nFROM (\n"
                    "    SELECT lcsc,\n           "
                    (str/join ",\n           "
                              (for [[alias path] attrs]
                                (str "json_extract(attributes, '" path "') AS " alias)))
                    "\n    FROM out.components\n"
                    "    WHERE " member "\n"
                    ")\n"
                    "WHERE substr(" guard-col ", -1) = '" guard-suffix "'\n"
                    ");")
        index (str "CREATE INDEX out.idx_" table "_" value-col
                   " ON " table " (" value-col ");")]
    [create insert index]))

(defn value-case-sql
  "CASE expression parsing a number-with-unit-suffix string into a REAL.
  Longest suffixes match first so 'mΩ' wins over bare 'Ω'; substr equality
  keeps the match case-sensitive.
  A CASE with no matching branch yields NULL, which is what unparseable input should give.
  extra-whens are complete WHEN clauses tried before the suffix branches."
  [col scales & extra-whens]
  (let [branch (fn [[suffix scale]]
                 (let [n (count suffix)]
                   (str "WHEN substr(" col ", -" n ") = '" suffix "'"
                        " THEN CAST(substr(" col ", 1, length(" col ") - " n ") AS REAL)"
                        (when-not (== 1.0 scale)
                          (str " * " scale)))))
        branches (concat extra-whens
                         (map branch (sort-by (fn [[suffix _]] (- (count suffix))) scales)))]
    (str "CASE " (str/join "\n            " branches) " END")))

(defn fraction-when-sql
  "Vulgar fractions like '1/4W' -> 0.25.
  A plain CAST of '1/4' reads as 1.0, so divide the two parts explicitly."
  [col]
  (str "WHEN instr(" col ", '/') > 0 AND substr(" col ", -1) = 'W'"
       " THEN CAST(substr(" col ", 1, instr(" col ", '/') - 1) AS REAL)"
       " / CAST(substr(" col ", instr(" col ", '/') + 1, length(" col ") - instr(" col ", '/') - 1) AS REAL)"))

(defn tolerance-sql
  "Symmetric '±X%' -> X; asymmetric forms like '-20%~+80%' become NULL."
  [col]
  (str "CASE WHEN substr(" col ", 1, 1) = '±' AND substr(" col ", -1) = '%'"
       " THEN CAST(substr(" col ", 2, length(" col ") - 2) AS REAL) END"))

(defn tempco-sql
  "'±100ppm/℃' -> 100.0"
  [col]
  (str "CASE WHEN substr(" col ", 1, 1) = '±' AND substr(" col ", -5) = 'ppm/℃'"
       " THEN CAST(substr(" col ", 2, length(" col ") - 6) AS REAL) END"))

(defn dielectric-sql
  "For ceramics the upstream Temperature Coefficient holds the dielectric class;
  keep it only when it looks like one (X7R, C0G, NP0, ...)."
  [col]
  (str "CASE WHEN " col " GLOB '[A-Z][0-9][A-Z]'"
       " OR " col " GLOB '[A-Z][0-9][A-Z][A-Z]'"
       " OR " col " = 'NP0'"
       " THEN " col " END"))

(defn canon-sql
  "NULL-preserving canonicalization of a REAL through printf('%.6e') and re-parse,
  so decimal-equal values land on the identical double regardless of source unit."
  [expr]
  (str "IIF(" expr " IS NULL, NULL, CAST(printf('%.6e', " expr ") AS REAL))"))

(defn sql-quote [s]
  (str/replace s "'" "''"))

;; ---- pure core: reporting ----

(defn plan-summary
  "The confirmation report as a seq of lines."
  [{:keys [part-names total-bytes work-dir output existing]}]
  (let [dl-mb (quot total-bytes 1000000)
        ;; The zip inflates to roughly 9x its compressed size, and the compressed
        ;; archive is held alongside it briefly, so ~10x download size covers the peak.
        scratch-gb (quot (+ (* total-bytes 10) 999999999) 1000000000)]
    ["This will:"
     (str "  - download " (count part-names) " files, ~" dl-mb " MB total, from " base-url "/")
     (str "  - use up to ~" scratch-gb " GB of scratch space in " work-dir)
     (cond
       (nil? existing)
       (str "  - write the finished database to " output)
       (:generated-at existing)
       (str "  - replace the database at " output
            " (built " (:generated-at existing) ", " (or (:row-count existing) "?") " parts)")
       :else
       (str "  - overwrite " output ", which exists but is not a recognized parts database"))]))

(defn now-iso []
  (str (.truncatedTo (java.time.Instant/now) java.time.temporal.ChronoUnit/SECONDS)))

;; ---- imperative shell ----

(defn discover-parts!
  "The split archive is cache.z01 .. cache.zNN plus a final cache.zip; the part count
  grows as the catalog grows, so probe with HEAD requests until the first 404,
  summing the reported sizes so the confirmation can show real numbers."
  []
  (println "Discovering archive parts...")
  (loop [i 1 names [] total 0]
    (let [nm (format "cache.z%02d" i)]
      (if-let [size (head-size! nm)]
        (recur (inc i) (conj names nm) (+ total size))
        (if-let [zip-size (head-size! "cache.zip")]
          {:part-names (conj names "cache.zip")
           :total-bytes (+ total zip-size)}
          (die! (str "cannot reach " base-url "/cache.zip")))))))

(defn head-size!
  "Content-length of one archive part, or nil when it does not exist."
  [nm]
  (let [resp (http/head (str base-url "/" nm) {:throw false})]
    (when (= 200 (:status resp))
      (or (some-> (get-in resp [:headers "content-length"]) parse-long) 0))))

(defn existing-db-meta!
  "-> nil when path does not exist; {:generated-at nil} when it exists but is not
  a recognized parts database; both meta values otherwise. Opens read-only."
  [path]
  (when (fs/exists? path)
    (let [q (fn [k]
              (let [{:keys [exit out]}
                    (p/shell {:continue true :out :string :err :string}
                             "sqlite3" "-readonly" (str path)
                             (str "SELECT value FROM meta WHERE key='" k "';"))]
                (when (zero? exit)
                  (not-empty (str/trim out)))))]
      {:generated-at (q "generated_at")
       :row-count (q "row_count")})))

(defn confirm!
  "Ask only when stdin and stdout are TTYs; default answer is yes."
  []
  (when (tty?)
    (print "Continue? [Y/n] ")
    (flush)
    (let [reply (or (read-line) "")]
      (when (re-find #"^[nN]" reply)
        (binding [*out* *err*] (println "Aborted."))
        (System/exit 1)))))

(defn tty? []
  ;; The child test(1) inherits this process's stdin/stdout, so it checks the same fds.
  (zero? (:exit (p/shell {:continue true} "test" "-t" "0" "-a" "-t" "1"))))

(defn download! [part-names work-dir]
  (println "Downloading JLCPCB parts database...")
  ;; --fail so an HTTP error page is not silently saved as if it were archive data.
  (doseq [nm part-names]
    (let [url (str base-url "/" nm)
          {:keys [exit]} (p/shell {:continue true}
                                  "curl" "-fL" "--retry" "3" "--retry-delay" "5"
                                  "-o" (str (fs/path work-dir nm)) url)]
      (when-not (zero? exit)
        (die! (str "download failed: " url))))))

(defn extract!
  "Concatenate the parts in order (numbered parts first, cache.zip last) into the
  whole archive, unzip, and validate; returns the raw database path.
  unzip exits nonzero with warnings about the concatenated split archive but still
  extracts it correctly, so ignore its exit code and validate the result instead."
  [part-names work-dir]
  (println "Extracting...")
  (let [combined (fs/path work-dir "combined.zip")
        raw-file (fs/path work-dir "cache.sqlite3")]
    (with-open [out (io/output-stream (fs/file combined))]
      (doseq [nm part-names]
        (io/copy (fs/file (fs/path work-dir nm)) out)))
    (doseq [nm part-names]
      (fs/delete (fs/path work-dir nm)))
    (p/shell {:continue true} "unzip" "-q" "-o" (str combined) "-d" (str work-dir))
    (fs/delete combined)
    (when-not (sqlite-file? raw-file)
      (die! (str "the extracted file is not a SQLite database: " base-url "/cache.zip")))
    raw-file))

(defn sqlite-file? [path]
  (and (fs/exists? path)
       (with-open [in (io/input-stream (fs/file path))]
         (let [buf (byte-array 15)]
           (and (= 15 (.read in buf))
                (= "SQLite format 3" (String. buf 0 15 "UTF-8")))))))

(defn build!
  "Run the transform, drop the raw database, VACUUM, and check the row count;
  returns the row count as a string."
  [raw-file new-file generated-at output]
  (println "Building materialized components table...")
  (p/shell {:in (build-sql {:new-file new-file :generated-at generated-at})}
           "sqlite3" (str raw-file))
  ;; The raw ~5.7 GB extracted database is no longer needed once we've materialized.
  (delete-with-siblings! raw-file)
  (println "Compacting...")
  (p/shell "sqlite3" (str new-file) "VACUUM;")
  (let [row-count (-> (p/shell {:out :string} "sqlite3" (str new-file)
                               "SELECT COUNT(*) FROM components;")
                      :out str/trim)]
    (when (= "0" row-count)
      (die! (str "the new database has no components; keeping the old " output)))
    row-count))

(defn install!
  "Only now is the old database replaced. The scratch directory may be on a
  different filesystem than the output, so the file is first staged as a sibling
  of the output and then renamed into place, keeping the final step atomic."
  [new-file output]
  (let [stage-file (str output ".new")]
    (fs/delete-if-exists stage-file)
    (fs/move new-file stage-file)
    (delete-with-siblings! output)
    (fs/move stage-file output)))

(defn delete-with-siblings!
  "Delete a database file plus any -wal / -journal / -shm siblings."
  [path]
  (fs/delete-if-exists path)
  (let [dir (or (fs/parent path) (fs/path "."))
        prefix (str (fs/file-name path) "-")]
    (doseq [f (fs/list-dir dir)
            :when (str/starts-with? (fs/file-name f) prefix)]
      (fs/delete f))))

(defn du-h! [path]
  (-> (p/shell {:out :string} "du" "-h" "--" (str path))
      :out (str/split #"\s") first))

(defn die! [msg]
  (binding [*out* *err*] (println (str "Error: " msg)))
  (System/exit 1))

(when (= *file* (System/getProperty "babashka.file"))
  (apply -main *command-line-args*))
