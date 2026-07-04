(ns kicad-analyzer.core-test
  (:require
   [clojure.test :refer [deftest is testing]]
   [datascript.core :as d]
   [kicad-analyzer.core :as kicad]))

(def sample-netlist
  "(export
     (version \"E\")
     (components
       (comp
         (ref \"C1\")
         (value \"10nF\")
         (footprint \"pio-footprints:CAPC320X160X180L55N\")
         (description \"CAP CER 10000PF 100V C0G 1206\")
         (fields
           (field (name \"Used\") \"Yes\")
           (field (name \"Voltage\") \"100V\")
           (field (name \"FT Rotation Offset\") \"180\")
           (field (name \"Datasheet\")))
         (libsource
           (lib \"pio-dblib\")
           (part \"Capacitors/101-00002\")
           (description \"CAP CER 10000PF 100V C0G 1206\"))
         (property (name \"Used\") (value \"Yes\"))
         (property (name \"FT Rotation Offset\") (value \"180\"))
         (property (name \"ki_fp_filters\") (value \"C_1206*\"))
         (property (name \"Sheetfile\") (value \"eye-spy.kicad_sch\"))
         (sheetpath (names \"/\") (tstamps \"/\"))
         (tstamps \"5f9eda4b-f430-40d8-9c73-60ef2b2cec89\"))
       (comp
         (ref \"U1\")
         (value \"MCU\")
         (footprint \"Package_QFP:LQFP-48\")
         (libsource
           (lib \"MCU_Microchip_ATmega\")
           (part \"ATmega32U4-A\"))))
     (nets
       (net
         (code \"1\")
         (name \"/RESET\")
         (node
           (ref \"C1\")
           (pin \"1\")
           (pinfunction \"1\")
           (pintype \"passive\"))
         (node
           (ref \"U1\")
           (pin \"13\")
           (pinfunction \"~{RESET}\")
           (pintype \"input\")))
       (net
         (code \"2\")
         (name \"GND\")
         (node
           (ref \"C1\")
           (pin \"2\")
           (pinfunction \"2\")
           (pintype \"passive\")))))")

(deftest parse-netlist-test
  (is (= {:symbols [{:symbol/id "pio-dblib:Capacitors/101-00002"
                     :symbol/lib "pio-dblib"
                     :symbol/part "Capacitors/101-00002"
                     :symbol/description "CAP CER 10000PF 100V C0G 1206"}
                    {:symbol/id "MCU_Microchip_ATmega:ATmega32U4-A"
                     :symbol/lib "MCU_Microchip_ATmega"
                     :symbol/part "ATmega32U4-A"}]
          :instances [{:instance/ref "C1"
                       :instance/value "10nF"
                       :instance/footprint "pio-footprints:CAPC320X160X180L55N"
                       :instance/description "CAP CER 10000PF 100V C0G 1206"
                       :instance/symbol [:symbol/id "pio-dblib:Capacitors/101-00002"]
                       :instance/attributes [{:attribute/name "Used"
                                              :attribute/value "Yes"}
                                             {:attribute/name "Voltage"
                                              :attribute/value "100V"}
                                             {:attribute/name "FT Rotation Offset"
                                              :attribute/value "180"}]
                       :instance/sheetpath "/"
                       :instance/pins [{:pin/number "1"
                                        :pin/function "1"
                                        :pin/type "passive"}
                                       {:pin/number "2"
                                        :pin/function "2"
                                        :pin/type "passive"}]}
                      {:instance/ref "U1"
                       :instance/value "MCU"
                       :instance/footprint "Package_QFP:LQFP-48"
                       :instance/symbol [:symbol/id "MCU_Microchip_ATmega:ATmega32U4-A"]
                       :instance/attributes []
                       :instance/pins [{:pin/number "13"
                                        :pin/function "~{RESET}"
                                        :pin/type "input"}]}]
          :nets [{:net/name "/RESET"
                  :net/nodes [{:node/ref "C1"
                               :node/pin-number "1"}
                              {:node/ref "U1"
                               :node/pin-number "13"}]}
                 {:net/name "GND"
                  :net/nodes [{:node/ref "C1"
                               :node/pin-number "2"}]}]}
         (kicad/parse-netlist sample-netlist))))

(def footprintless-netlist
  "A schematic mid-design can contain parts with no footprint assigned yet;
   kicad-cli then emits the comp without a footprint value."
  "(export
     (version \"E\")
     (components
       (comp
         (ref \"J1\")
         (value \"Conn_01x02_Socket\")
         (footprint)
         (libsource
           (lib \"Connector\")
           (part \"Conn_01x02_Socket\"))
         (sheetpath (names \"/\") (tstamps \"/\"))))
     (nets
       (net
         (code \"1\")
         (name \"GND\")
         (node
           (ref \"J1\")
           (pin \"1\")
           (pintype \"passive\")))))")

(deftest footprintless-component-test
  (testing "a part without a footprint parses without a nil footprint entry"
    (let [[instance] (:instances (kicad/parse-netlist footprintless-netlist))]
      (is (not (contains? instance :instance/footprint)))))

  (testing "a part without a footprint loads into the db"
    (let [db (kicad/netlist->db footprintless-netlist)]
      (is (= #{["J1"]}
             (d/q '[:find ?ref
                    :where [_ :instance/ref ?ref]]
                  db))))))

(deftest datascript-db-test
  (let [db (kicad/netlist->db sample-netlist)]
    (testing "instances are queryable by reference"
      (is (= #{["C1" "10nF"]
               ["U1" "MCU"]}
             (d/q '[:find ?ref ?value
                    :where
                    [?e :instance/ref ?ref]
                    [?e :instance/value ?value]]
                  db))))

    (testing "fields and properties are collapsed into owned attribute entities"
      (is (= #{["Voltage" "100V"]
               ["FT Rotation Offset" "180"]}
             (d/q '[:find ?name ?value
                    :where
                    [?instance :instance/ref "C1"]
                    [?instance :instance/attributes ?attribute]
                    [?attribute :attribute/name ?name]
                    [?attribute :attribute/value ?value]
                    [(contains? #{"Voltage" "FT Rotation Offset"} ?name)]]
                  db))))

    (testing "pins are owned by instances"
      (is (= "13"
             (:pin/number (kicad/instance-pin db "U1" "13")))))

    (testing "net names are queryable from instance pins"
      (is (= #{["/RESET"]}
             (d/q '[:find ?net-name
                    :where
                    [?instance :instance/ref "U1"]
                    [?instance :instance/pins ?pin]
                    [?pin :pin/number "13"]
                    [?net :net/nodes ?node]
                    [?node :node/pin ?pin]
                    [?net :net/name ?net-name]]
                  db))))))
