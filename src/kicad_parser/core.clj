(ns kicad-parser.core
  (:require
   [clojure.edn :as edn]
   [clojure.java.io :as io]
   clojure.pprint
   [clojure.string :as str]
   [datascript.core :as d]))


(defmethod print-method datascript.impl.entity.Entity [e ^java.io.Writer w]
  (.write w (pr-str (into {:db/id (:db/id e)} (d/touch e)))))

(defmethod clojure.pprint/simple-dispatch datascript.impl.entity.Entity [e]
  (clojure.pprint/simple-dispatch (into {} (d/touch e))))

(def schema
  {:symbol/id {:db/unique :db.unique/identity}
   :instance/ref {:db/unique :db.unique/identity}
   :instance/symbol {:db/valueType :db.type/ref}
   :instance/attribute {:db/valueType :db.type/ref
                        :db/cardinality :db.cardinality/many
                        :db/isComponent true}
   :instance/pin {:db/valueType :db.type/ref
                  :db/cardinality :db.cardinality/many
                  :db/isComponent true}
   :net/name {:db/unique :db.unique/identity}
   :net/node {:db/valueType :db.type/ref
              :db/cardinality :db.cardinality/many
              :db/isComponent true}
   :node/pin {:db/valueType :db.type/ref}})

(defn- tagged?
  [tag node]
  (and (seq? node)
       (= tag (first node))))

(defn- child
  [tag node]
  (first (filter #(tagged? tag %) (rest node))))

(defn- children
  [tag node]
  (filter #(tagged? tag %) (rest node)))

(defn- child-value
  [tag node]
  (second (child tag node)))

(defn- named-value
  [node]
  [(child-value 'name node)
   (child-value 'value node)])

(defn- field-value
  [node]
  [(child-value 'name node)
   (first (filter string? (rest node)))])

(defn- entries
  [entry-fn nodes]
  (into {}
        (keep (fn [node]
                (let [[k v] (entry-fn node)]
                  (when k
                    [k v]))))
        nodes))

(defn- libsource
  [component]
  (when-let [source (child 'libsource component)]
    {:lib (child-value 'lib source)
     :part (child-value 'part source)
     :description (child-value 'description source)}))

(defn- sheetpath
  [component]
  (when-let [path (child 'sheetpath component)]
    (child-value 'names path)))

(defn- symbol-id
  [{:keys [lib part]}]
  (str lib ":" part))

(defn- symbol-entity
  [node]
  (when-let [source (libsource node)]
    (cond-> {:symbol/id (symbol-id source)
             :symbol/lib (:lib source)
             :symbol/part (:part source)}
      (:description source) (assoc :symbol/description (:description source)))))

(def ^:private ignored-attribute-names
  #{"Description"
    "Footprint"
    "Sheetfile"
    "Sheetname"
    "ki_fp_filters"
    "ki_keywords"})

(defn- attribute
  [[name value]]
  (when (some? value)
    {:attribute/name name
     :attribute/value value}))

(defn- attributes
  [node]
  (let [fields-node (child 'fields node)]
    (->> (concat
          (entries field-value (children 'field fields-node))
          (entries named-value (children 'property node)))
         (remove (fn [[name _value]]
                   (contains? ignored-attribute-names name)))
         (keep attribute)
         distinct
         vec)))

(defn- net-node
  [node]
  {:node/ref (child-value 'ref node)
   :node/pin-number (child-value 'pin node)})

(defn- net-node-pin
  [node]
  (cond-> {:pin/number (child-value 'pin node)}
    (child-value 'pinfunction node) (assoc :pin/function (child-value 'pinfunction node))
    (child-value 'pintype node) (assoc :pin/type (child-value 'pintype node))))

(defn- pins-by-ref
  [nets-node]
  (->> (children 'net nets-node)
       (mapcat #(children 'node %))
       (group-by #(child-value 'ref %))
       (map (fn [[ref nodes]]
              [ref (->> nodes
                        (map net-node-pin)
                        (distinct)
                        (sort-by :pin/number)
                        vec)]))
       (into {})))

(defn- instance
  [pins-by-ref node]
  (let [source (libsource node)
        sheetpath (sheetpath node)]
    (cond-> {:instance/ref (child-value 'ref node)
             :instance/value (child-value 'value node)
             :instance/footprint (child-value 'footprint node)
             :instance/attributes (attributes node)
             :instance/pins (get pins-by-ref (child-value 'ref node) [])}
      (child-value 'description node) (assoc :instance/description (child-value 'description node))
      source (assoc :instance/symbol [:symbol/id (symbol-id source)])
      sheetpath (assoc :instance/sheetpath sheetpath))))

(defn- net
  [node]
  {:net/name (child-value 'name node)
   :net/nodes (mapv net-node (children 'node node))})

(defn parse-netlist
  [netlist-text]
  (let [netlist (edn/read-string netlist-text)
        components-node (child 'components netlist)
        component-nodes (children 'comp components-node)
        nets-node (child 'nets netlist)
        pins-by-ref (pins-by-ref nets-node)]
    {:symbols (vec (keep symbol-entity component-nodes))
     :instances (mapv #(instance pins-by-ref %) component-nodes)
     :nets (mapv net (children 'net nets-node))}))

(defn parse-components
  [netlist-text]
  (:instances (parse-netlist netlist-text)))

(defn- instance-tx
  [instance]
  (-> instance
      (dissoc :instance/attributes :instance/pins)
      (assoc :instance/attribute (:instance/attributes instance)
             :instance/pin (:instance/pins instance))))

(defn- base-tx
  [{:keys [symbols instances]}]
  (concat
   (keep identity symbols)
   (map instance-tx instances)))

(defn instance-pin
  [db instance-ref pin-number]
  (when-let [pin-id (d/q '[:find ?pin .
                           :in $ ?ref ?pin-number
                           :where
                           [?instance :instance/ref ?ref]
                           [?instance :instance/pin ?pin]
                           [?pin :pin/number ?pin-number]]
                         db instance-ref pin-number)]
    (d/entity db pin-id)))

(defn- resolved-net-node
  [db {:keys [node/ref node/pin-number]}]
  (if-let [pin (instance-pin db ref pin-number)]
    {:node/pin (:db/id pin)}
    (throw (ex-info "Net references an unknown instance pin"
                    {:instance-ref ref
                     :pin-number pin-number}))))

(defn- net-tx
  [db {:net/keys [name nodes]}]
  {:net/name name
   :net/node (mapv #(resolved-net-node db %) nodes)})

(defn netlist-data->db
  [netlist-data]
  (let [conn (d/create-conn schema)]
    (d/transact! conn (base-tx netlist-data))
    (d/transact! conn (mapv #(net-tx @conn %) (:nets netlist-data)))
    @conn))

(defn components->db
  [components]
  (netlist-data->db {:symbols []
                     :instances components
                     :nets []}))

(defn netlist->db
  [netlist-text]
  (netlist-data->db (parse-netlist netlist-text)))

(defn- executable?
  [path]
  (when path
    (let [file (io/file path)]
      (and (.exists file)
           (.canExecute file)
           (.getPath file)))))

(defn- successful?
  [{:keys [exit]}]
  (zero? exit))

(defn- run-command
  [command]
  (let [process (-> (ProcessBuilder. command)
                    (.redirectErrorStream true)
                    (.start))
        out (slurp (.getInputStream process))
        exit (.waitFor process)]
    {:exit exit
     :out out}))

(defn- flatpak-kicad?
  []
  (successful? (run-command ["flatpak" "info" "org.kicad.KiCad"])))

(defn kicad-cli-command
  []
  (let [os-name (str/lower-case (System/getProperty "os.name"))
        mac-cli (or (executable? "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
                    (executable? "/Applications/KiCad/kicad.app/Contents/MacOS/kicad-cli"))]
    (cond
      (and (str/includes? os-name "linux") (flatpak-kicad?))
      ["flatpak" "run" "--command=kicad-cli" "org.kicad.KiCad"]

      (and (str/includes? os-name "mac") mac-cli)
      [mac-cli]

      :else
      ["kicad-cli"])))

(defn export-netlist
  [schematic-path]
  (let [schematic-file (io/file schematic-path)
        output-dir (or (.getParentFile (.getAbsoluteFile schematic-file))
                       (io/file "."))
        out-file (java.io.File/createTempFile "kicad-parser-" ".net" output-dir)
        command (into (kicad-cli-command)
                      ["sch" "export" "netlist"
                       "--format" "kicadsexpr"
                       "--output" (.getPath out-file)
                       schematic-path])
        {:keys [exit out]} (run-command command)]
    (try
      (when-not (zero? exit)
        (throw (ex-info "KiCad netlist export failed"
                        {:command command
                         :exit exit
                         :output out})))
      (slurp out-file)
      (finally
        (.delete out-file)))))

(defn schematic->db
  [schematic-path]
  (netlist->db (export-netlist schematic-path)))

(defn -main
  [& args]
  (let [[schematic-path] args]
    (when-not schematic-path
      (binding [*out* *err*]
        (println "Usage: clojure -M -m kicad-parser.core path/to/design.kicad_sch"))
      (System/exit 2))
    (print (export-netlist schematic-path))))


(comment
  (def db
    (schematic->db "../plate-reader/pcbs/receiver/receiver.kicad_sch"))

  (d/touch (d/entity db [:instance/ref "U1"]))

  (d/q '{:find [?s]
         :where [[_ :instance/symbol ?s]]}
       db)


  (zipmap (range 100) (repeat :foo))

;;
  )
