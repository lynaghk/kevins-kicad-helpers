(ns user
  (:require
   [kaocha.repl :as kaocha]
   [kicad-parser.core]))

(defn test!
  []
  (kaocha/run-all))
