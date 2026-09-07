(ns project-tasks.check
  (:refer-clojure :exclude [run!])
  (:require [babashka.fs :as fs]
            [babashka.process :as p]
            [project-tasks.analysis :as analysis]
            [project-tasks.schematic :as schematic]
            [project-tasks.shared :as shared]))

(declare run-stages kicad-check!)

(defn run!
  "Run every check for the project and stop at the first failure.
  Returns {:ok? bool :checks [{:label :ok? :detail :report}]} where :detail is
  a display string for a failed check and :report the absolute report path of
  a failed kicad check."
  [repo-dir {:keys [project-dir project-file project-name schematic pcb]}]
  (let [version (shared/build-version repo-dir)
        reports (fs/path project-dir "outputs" version "reports")]

    (fs/create-dirs reports)

    ;; Placeholder so interactive KiCad (which has no -D equivalent) resolves
    ;; ${KKH_VERSION_DATE}; meant to be committed alongside the board.
    (shared/ensure-text-variable! project-file "KKH_VERSION_DATE" "0000-00-00-unbuilt")

    (run-stages
     [{:label "analysis"
       :run! (fn []
               (let [components (schematic/load-components project-dir schematic)
                     findings (analysis/run-checks {:project-dir project-dir
                                                    :project-name project-name
                                                    :schematic schematic
                                                    :pcb pcb
                                                    :components components})]
                 (when (seq findings)
                   (shared/fail! (analysis/format-findings findings)))))}

      {:label "schematic"
       :run! #(shared/shell! project-dir ["kkh-analyze-schematic" (str schematic)])}

      {:label "ERC"
       :run! (fn []
               (let [report (fs/path reports (str project-name "-erc.txt"))]
                 (kicad-check!
                  project-dir
                  report
                  ["sch" "erc"
                   "--exit-code-violations"
                   "-o" (str report)
                   (str schematic)])))}

      ;; The real version, not the placeholder, so DRC checks the rendered
      ;; geometry of the text that will actually be plotted.
      {:label "DRC"
       :run! (fn []
               (let [report (fs/path reports (str project-name "-drc.txt"))]
                 (kicad-check!
                  project-dir
                  report
                  ["pcb" "drc"
                   "--schematic-parity"
                   "--exit-code-violations"
                   "-D" (str "KKH_VERSION_DATE=" version)
                   "-o" (str report)
                   (str pcb)])))}])))

(defn- run-stage [{:keys [label run!]}]
  (try
    (run!)
    {:label label :ok? true}
    (catch Exception error
      (merge {:label label :ok? false :detail (.getMessage error)}
             (select-keys (ex-data error) [:report])))))

(defn run-stages [stages]
  (loop [stages stages
         results []]
    (if-let [stage (first stages)]
      (let [result (run-stage stage)]
        (if (:ok? result)
          (recur (rest stages) (conj results result))
          {:ok? false :checks (conj results result)}))
      {:ok? true :checks results})))

(defn- kicad-check! [project-dir report args]
  (let [{:keys [exit]} @(p/process (into ["kicad-cli"] args)
                                   {:dir (str project-dir)
                                    :out :string
                                    :err :string})]
    (when-not (zero? exit)
      (shared/fail! (shared/path-for-message (fs/cwd) report)
                    {:report (str report)}))))
