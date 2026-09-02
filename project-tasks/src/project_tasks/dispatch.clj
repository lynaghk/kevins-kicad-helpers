(ns project-tasks.dispatch
  (:refer-clojure :exclude [run!])
  (:require [clojure.string :as str]
            [project-tasks.build :as build]
            [project-tasks.check :as check]
            [project-tasks.shared :as shared]))

(def usage
  (str "Usage:\n"
       "  kkh list\n"
       "  kkh check [board]\n"
       "  kkh build [board] [--force]\n"
       "  kkh macos-opener install | status | uninstall\n"
       "\n"
       "Boards whose directory contains a .kkh-skip file are skipped unless named explicitly."))

(defn board-line [{:keys [board-name skip?]}]
  (str board-name (when skip? " (skipped)")))

(defn available-boards [projects]
  (str/join "\n" (map #(str "  " (board-line %)) projects)))

(defn fail-with-projects! [message projects]
  (shared/fail! (str message "\n\n" usage "\n\nAvailable PCBs:\n"
                     (available-boards projects))))

(defn parse-action [action args projects]
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
      {:force? force?
       :all? (nil? target)
       :target target})))

(defn select-projects [{:keys [all? target]} projects]
  (if all?
    (vec (remove :skip? projects))
    [(or (some #(when (= target (:board-name %)) %) projects)
         (fail-with-projects! (str "Unknown PCB: " target) projects))]))

(defn run-project! [repo-dir action force? project]
  (println (str "\nPCB " (:board-name project) ": " action))
  (try
    (case action
      "build" (build/run! repo-dir project force?)
      "check" (check/run! repo-dir project))
    {:board-name (:board-name project)
     :success? true}
    (catch Exception error
      (when-not (:kkh/complete-message? (ex-data error))
        (println (str "PCB " (:board-name project) " " action " failed:")))
      (println (.getMessage error))
      {:board-name (:board-name project)
       :success? false})))

(defn run-command! [repo-dir action args projects]
  (let [{:keys [force? all?] :as options} (parse-action action args projects)
        selected-projects (select-projects options projects)
        results (mapv #(run-project! repo-dir action force? %) selected-projects)]
    (if (every? :success? results) 0 1)))

(defn run! [command args]
  (try
    (let [repo-dir (shared/repo-dir)
          projects (shared/discover-projects repo-dir)]
      (case command
        "list" (if (empty? args)
                 (do
                   (doseq [project projects]
                     (println (board-line project)))
                   0)
                 (fail-with-projects! "list does not accept arguments." projects))
        "build" (run-command! repo-dir "build" args projects)
        "check" (run-command! repo-dir "check" args projects)
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
