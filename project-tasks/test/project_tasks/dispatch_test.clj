(ns project-tasks.dispatch-test
  (:require [clojure.test :refer [deftest is]]
            [project-tasks.dispatch :as dispatch]))

(def projects
  [{:board-name "pcbs/alpha" :skip? false :project-dir "/repo/pcbs/alpha"}
   {:board-name "pcbs/beta" :skip? true :project-dir "/repo/pcbs/beta"}])

(deftest argless-action-inside-board-dir-targets-that-board
  (is (= {:force? false :all? false :target "pcbs/alpha" :cwd-default? true}
         (dispatch/parse-action "check" [] projects "/repo/pcbs/alpha/docs"))))

(deftest argless-action-inside-skipped-board-dir-targets-it
  (is (= "pcbs/beta"
         (:target (dispatch/parse-action "check" [] projects "/repo/pcbs/beta")))))

(deftest argless-action-at-repo-root-selects-all
  (is (= {:force? false :all? true}
         (dispatch/parse-action "check" [] projects "/repo"))))

(deftest explicit-target-overrides-cwd-board
  (is (= {:force? false :all? false :target "pcbs/beta"}
         (dispatch/parse-action "check" ["pcbs/beta"] projects "/repo/pcbs/alpha"))))

(deftest argless-selection-excludes-skipped-boards
  (is (= ["pcbs/alpha"]
         (map :board-name (dispatch/select-projects {:all? true} projects)))))

(deftest explicit-target-selects-skipped-board
  (is (= ["pcbs/beta"]
         (map :board-name
              (dispatch/select-projects {:all? false :target "pcbs/beta"}
                                        projects)))))

(deftest board-line-marks-skipped-boards
  (is (= "pcbs/alpha" (dispatch/board-line (first projects))))
  (is (= "pcbs/beta (skipped)" (dispatch/board-line (second projects)))))
