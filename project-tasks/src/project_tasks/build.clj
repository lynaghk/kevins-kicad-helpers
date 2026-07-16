(ns project-tasks.build
  (:refer-clojure :exclude [run!])
  (:require [babashka.fs :as fs]
            [clojure.data.csv :as csv]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [project-tasks.check :as check]
            [project-tasks.shared :as shared]))

(defn- read-csv [path]
  (csv/read-csv (str/replace-first (slurp (str path)) "\uFEFF" "")))

(defn filter-positions-to-bom!
  "Drop rows from positions.csv for parts the BOM does not list, e.g. footprints
  that only have the exclude-from-BOM fabrication attribute set (mounting holes).
  The Fabrication Toolkit writes no bom.csv at all when every footprint is
  excluded (nothing to assemble), so a missing file means an empty BOM."
  [toolkit-outputs]
  (let [bom (fs/path toolkit-outputs "bom.csv")
        in-bom (if (fs/exists? bom)
                 (->> (read-csv bom)
                      rest
                      (into #{} (mapcat #(str/split (first %) #",\s*"))))
                 #{})
        positions (fs/path toolkit-outputs "positions.csv")
        [header & rows] (read-csv positions)]
    (with-open [writer (io/writer (fs/file positions))]
      (.write writer "\uFEFF")
      (csv/write-csv writer (cons header (filter (comp in-bom first) rows))))))

(defn inner-copper-layers
  "Inner copper layer names from the PCB's board layer table, in stackup order.
  Matching the numeric-id tuple form (<id> \"In<n>.Cu\" signal) keeps footprint
  pads' quoted (layers ...) lists from matching."
  [pcb]
  (into [] (map second) (re-seq #"\(\d+ \"(In\d+\.Cu)\"" (slurp (str pcb)))))

(defn- diagnostic-line [line]
  (some->> (re-matches #"\[[^\]]+\]:\s*(.+)" (str/trim line))
           second
           str/trim
           not-empty))

(defn- summary-line [line]
  (some->> (re-matches #"\*\*\s+(.+)" (str/trim line))
           second
           str/trim
           not-empty))

(defn- report-details [report]
  (when (fs/regular-file? report)
    (let [lines (str/split-lines (slurp (str report)))]
      {:summary (some summary-line lines)
       :preview (->> lines
                     (keep diagnostic-line)
                     (take 3)
                     seq)})))

(defn- path-for-message [repo-dir path]
  (let [repo-dir (fs/absolutize repo-dir)
        path (fs/absolutize path)]
    (try
      (str (fs/relativize repo-dir path))
      (catch Exception _
        (str path)))))

(defn format-check-failure [repo-dir {:keys [board-name]} check-failure]
  (let [{:keys [check-label report]} check-failure
        {:keys [summary preview]} (report-details report)]
    (str "Could not build " board-name " because " check-label " failed.\n"
         "\n"
         "First errors from:\n"
         "  " (path-for-message repo-dir report) "\n"
         (when summary
           (str "  " summary "\n"))
         "\n"
         (if (seq preview)
           (str/join "\n" (map-indexed #(str "  " (inc %1) ". " %2) preview))
           "  Report could not be previewed.")
         "\n"
         "\n"
         "Fix these issues, inspect the report for the complete list, or rerun with --force.")))

(defn run! [repo-dir {:keys [project-dir project-file project-name schematic pcb] :as project} force?]
  (when-not force?
    (try
      (check/run! repo-dir project)
      (catch Exception error
        (if (= :kicad-check-failure (:kkh/error (ex-data error)))
          (shared/fail! (format-check-failure repo-dir project (ex-data error))
                        {:kkh/complete-message? true})
          (shared/fail! (str "Could not build " (:board-name project)
                             " because PCB checks failed.\n"
                             "\n"
                             (.getMessage error)
                             "\n"
                             "\n"
                             "Fix the reported issues, or rerun with --force.")
                        {:kkh/complete-message? true})))))
  (let [version (shared/build-version repo-dir)
        ;; Lets boards stamp the build version on the silkscreen by placing a
        ;; ${KKH_VERSION_DATE} text item.
        version-var (str "KKH_VERSION_DATE=" version)
        outputs (fs/path project-dir "outputs" version)
        schematics-outputs (fs/path outputs "schematics")
        toolkit-outputs (fs/path project-dir "production")]
    (fs/delete-tree outputs)
    (fs/create-dirs schematics-outputs)
    (shared/kicad-cli!
     project-dir
     ["sch" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-sch.pdf")))
      (str schematic)])
    ;; Autoscale (--scale 0) centers the board on the page; boards drawn off the
    ;; page origin (e.g. to line up DXF imports) would otherwise plot outside
    ;; the page box and get cropped.
    (shared/kicad-cli!
     project-dir
     ["pcb" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-pcb-front.pdf")))
      "--scale" "0"
      "-D" version-var
      "-l" "F.Cu,F.Mask,F.Silkscreen,Edge.Cuts,"
      (str pcb)])
    (doseq [layer (inner-copper-layers pcb)]
      (shared/kicad-cli!
       project-dir
       ["pcb" "export" "pdf"
        "-o" (str (fs/path schematics-outputs
                           (str project-name "-pcb-"
                                (str/lower-case (fs/strip-ext layer)) ".pdf")))
        "--scale" "0"
        "-D" version-var
        "-l" (str layer ",Edge.Cuts,")
        (str pcb)]))
    (shared/kicad-cli!
     project-dir
     ["pcb" "export" "pdf"
      "-o" (str (fs/path schematics-outputs (str project-name "-pcb-back.pdf")))
      "--scale" "0"
      "--erd"
      "--ev"
      "--mirror"
      "-D" version-var
      "-l" "B.Cu,B.Mask,B.Silkscreen,Edge.Cuts,"
      (str pcb)])
    ;; Emit both the simplified (bounding-box components) and full-fidelity STEP
    ;; models. --keep-full leaves the intermediate full export next to the
    ;; simplified one as <project-name>.full.step.
    (shared/step-export!
     project-dir
     ["--keep-full"
      "-o" (str (fs/path outputs (str project-name ".simplified.step")))
      (str pcb)])
    ;; Fabrication Toolkit resolves text variables through pcbnew and has no
    ;; -D equivalent, so define the variable in the project file while it
    ;; plots. pcbnew prefers the board file's cached (property ...) copy of
    ;; the variable over the project file, so update that too.
    (shared/with-text-variable
      project-file "KKH_VERSION_DATE" version
      #(shared/with-board-text-variable
         pcb "KKH_VERSION_DATE" version
         (fn []
           (shared/run-fabrication-toolkit!
            project-dir
            pcb
            ["--autoTranslate"
             "--autoFill"
             "--excludeDNP"
             "--nonInteractive"]))))
    (filter-positions-to-bom! toolkit-outputs)
    (shared/copy-non-zip-contents! toolkit-outputs outputs)
    (fs/copy (shared/production-zip toolkit-outputs project-name)
             (fs/path outputs (str project-name "-gerbers-" version ".zip")))
    (fs/delete-tree toolkit-outputs)
    (println (str "Version: " version))
    (println (str "Outputs: " outputs))))
