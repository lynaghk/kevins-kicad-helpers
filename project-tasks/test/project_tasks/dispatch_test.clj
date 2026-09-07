(ns project-tasks.dispatch-test
  (:require [clojure.test :refer [deftest is]]
            [project-tasks.dispatch :as dispatch]))

(def projects
  [{:board-name "pcbs/alpha" :skip? false :project-dir "/repo/pcbs/alpha"}
   {:board-name "pcbs/beta" :skip? true :project-dir "/repo/pcbs/beta"}])

(deftest check-result-lines-collapse-a-passing-board
  (is (= ["✓ pcbs/pump"]
         (dispatch/check-result-lines
          "pcbs/pump"
          {:ok? true :checks [{:label "ERC" :ok? true}
                              {:label "DRC" :ok? true}]}))))

(deftest check-result-lines-show-hierarchy-for-a-failing-board
  (is (= ["pcbs/pump"
          "  ✓ analysis"
          "  ✓ ERC"
          "  ✗ DRC  outputs/v1/reports/pump-drc.rpt"]
         (dispatch/check-result-lines
          "pcbs/pump"
          {:ok? false :checks [{:label "analysis" :ok? true}
                               {:label "ERC" :ok? true}
                               {:label "DRC" :ok? false
                                :detail "outputs/v1/reports/pump-drc.rpt"}]}))))

(deftest check-result-lines-indent-multi-line-detail
  (is (= ["pcbs/pump"
          "  ✗ analysis"
          "      finding one"
          "      finding two"]
         (dispatch/check-result-lines
          "pcbs/pump"
          {:ok? false :checks [{:label "analysis" :ok? false
                                :detail "finding one\nfinding two"}]}))))

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
