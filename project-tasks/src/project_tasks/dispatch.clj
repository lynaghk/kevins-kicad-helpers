(ns project-tasks.dispatch
  (:refer-clojure :exclude [run!])
  (:require [babashka.fs :as fs]
            [clojure.string :as str]
            [project-tasks.build :as build]
            [project-tasks.check :as check]
            [project-tasks.shared :as shared]))

(def usage
  (str "Usage:\n"
       "  kkh list\n"
       "  kkh check [board]\n"
       "  kkh build [board] [--force]\n"
       "  kkh setup-kicad-libraries [--jlcpcb-version VERSION] [--yes]\n"
       "  kkh macos-opener install | status | uninstall\n"
       "\n"
       "check and build run every board unless one is named or the current directory is inside a board's folder.\n"
       "Boards whose directory contains a .kkh-skip file are skipped unless named explicitly or run from their folder."))

(defn board-line [{:keys [board-name skip?]}]
  (str board-name (when skip? " (skipped)")))

(defn available-boards [projects]
  (str/join "\n" (map #(str "  " (board-line %)) projects)))

(defn fail-with-projects! [message projects]
  (shared/fail! (str message "\n\n" usage "\n\nAvailable PCBs:\n"
                     (available-boards projects))))

(defn parse-action [action args projects cwd]
  (let [force-count (count (filter #{"--force"} args))
        force? (pos? force-count)
        targets (remove #{"--force"} args)]
    (when (> force-count 1)
      (fail-with-projects! "--force may only be specified once." projects))
    (when (and (= action "check") force?)
      (fail-with-projects! "--force is only valid with build." projects))
    (when (> (count targets) 1)
      (fail-with-projects! (str action " accepts at most one board name.") projects))
    (let [target (first targets)]
      (when (and target (str/starts-with? target "--"))
        (fail-with-projects! (str "Unknown argument: " target) projects))
      (if target
        {:force? force? :all? false :target target}
        (if-let [cwd-project (shared/project-containing-dir projects cwd)]
          {:force? force?
           :all? false
           :target (:board-name cwd-project)
           :cwd-default? true}
          {:force? force? :all? true})))))

(defn select-projects [{:keys [all? target]} projects]
  (if all?
    (vec (remove :skip? projects))
    [(or (some #(when (= target (:board-name %)) %) projects)
         (fail-with-projects! (str "Unknown PCB: " target) projects))]))

(defn check-result-lines
  "Board block for the check output: a passing board collapses to one line;
  a failing board lists each check that ran with its status and detail."
  [board-name {:keys [ok? checks]}]
  (if ok?
    [(str "✓ " board-name)]
    (into [board-name]
          (mapcat (fn [{:keys [label ok? detail]}]
                    (cond
                      ok?
                      [(str "  ✓ " label)]

                      (and detail (str/includes? detail "\n"))
                      (into [(str "  ✗ " label)]
                            (map #(str "      " %) (str/split-lines detail)))

                      :else
                      [(str "  ✗ " label (when detail (str "  " detail)))]))
                  checks))))

(defn run-project! [repo-dir action force? project]
  (try
    (case action
      "check" (let [result (check/run! repo-dir project)]
                (println)
                (doseq [line (check-result-lines (:board-name project) result)]
                  (println line))
                {:board-name (:board-name project)
                 :success? (:ok? result)})
      "build" (do
                (println (str "\n" (:board-name project) " build"))
                (build/run! repo-dir project force?)
                {:board-name (:board-name project)
                 :success? true}))
    (catch Exception error
      (when-not (:kkh/complete-message? (ex-data error))
        (println (str (:board-name project) " " action " failed:")))
      (println (.getMessage error))
      {:board-name (:board-name project)
       :success? false})))

(defn run-command! [repo-dir action args projects cwd]
  (let [{:keys [force? target cwd-default?] :as options} (parse-action action args projects cwd)
        selected-projects (select-projects options projects)]
    (when cwd-default?
      (println (str "Running only " target
                    " (current directory); run from the repo root for all boards.")))
    (let [results (mapv #(run-project! repo-dir action force? %) selected-projects)]
      (if (every? :success? results) 0 1))))

(defn run! [command args]
  (try
    (let [repo-dir (shared/repo-dir)
          projects (shared/discover-projects repo-dir)
          cwd (str (fs/cwd))]
      (case command
        "list" (if (empty? args)
                 (do
                   (doseq [project projects]
                     (println (board-line project)))
                   0)
                 (fail-with-projects! "list does not accept arguments." projects))
        "build" (run-command! repo-dir "build" args projects cwd)
        "check" (run-command! repo-dir "check" args projects cwd)
        (fail-with-projects!
         (str "Unknown command: " command)
         projects)))
    (catch Exception error
      (binding [*out* *err*]
        (println (.getMessage error)))
      1)))

(defn -main [& [command & args]]
  (cond
    (nil? command)
    (do (binding [*out* *err*] (println usage))
        (System/exit 1))

    (#{"help" "-h" "--help"} command)
    (do (println usage)
        (System/exit 0))

    :else
    (System/exit (run! command args))))
