(ns project-tasks.dispatch
  (:refer-clojure :exclude [run!])
  (:require [clojure.string :as str]
            [project-tasks.build :as build]
            [project-tasks.check :as check]
            [project-tasks.shared :as shared]))

(def usage
  (str "Usage:\n"
       "  bb list\n"
       "  bb check <board>\n"
       "  bb check --all\n"
       "  bb build <board> [--force]\n"
       "  bb build --all [--force]"))

(defn available-boards [projects]
  (str/join "\n" (map #(str "  " (:board-name %)) projects)))

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
    (when-not (= 1 (count targets))
      (fail-with-projects! (str action " expects exactly one board name or --all.") projects))
    (let [target (first targets)]
      (when (and (str/starts-with? target "--")
                 (not= "--all" target))
        (fail-with-projects! (str "Unknown argument: " target) projects))
      {:force? force?
       :all? (= "--all" target)
       :target target})))

(defn select-projects [{:keys [all? target]} projects]
  (if all?
    projects
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
      (println (str "PCB " (:board-name project) " " action " failed:"))
      (println (.getMessage error))
      {:board-name (:board-name project)
       :success? false})))

(defn print-summary! [results]
  (println "\nPCB summary:")
  (doseq [{:keys [board-name success?]} results]
    (println (str "  " board-name ": " (if success? "passed" "failed")))))

(defn run-command! [repo-dir action args projects]
  (let [{:keys [force? all?] :as options} (parse-action action args projects)
        selected-projects (select-projects options projects)
        results (mapv #(run-project! repo-dir action force? %) selected-projects)]
    (when all?
      (print-summary! results))
    (if (every? :success? results) 0 1)))

(defn run! [command args]
  (try
    (let [repo-dir (shared/repo-dir)
          projects (shared/discover-projects repo-dir)]
      (case command
        "list" (if (empty? args)
                 (do
                   (doseq [{:keys [board-name]} projects]
                     (println board-name))
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
