(ns project-tasks.dispatch-test
  (:require [clojure.test :refer [deftest is]]
            [project-tasks.dispatch :as dispatch]))

(def projects
  [{:board-name "pcbs/alpha" :skip? false}
   {:board-name "pcbs/beta" :skip? true}])

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
