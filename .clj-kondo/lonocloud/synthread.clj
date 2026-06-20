(ns lonocloud.synthread)

(defmacro if
  [x pred then else]
  `(clojure.core/let [x# ~x]
     (clojure.core/if ~pred
       (-> x# ~then)
       (-> x# ~else))))

(defmacro if-let
  [x [local pred] then else]
  `(clojure.core/let [x# ~x]
     (clojure.core/if-let [~local ~pred]
       (-> x# ~then)
       (-> x# ~else))))

(defmacro when
  [x pred & body]
  `(clojure.core/let [x# ~x]
     (clojure.core/if ~pred
       (-> x# ~@body)
       x#)))

(defmacro when-not
  [x pred & body]
  `(clojure.core/let [x# ~x]
     (clojure.core/if ~pred
       x#
       (-> x# ~@body))))

(defmacro when-let
  [x bindings & forms]
  `(clojure.core/let [x# ~x]
     (clojure.core/if-let ~bindings
       (-> x# ~@forms)
       x#)))

(defmacro let
  [x bindings & body]
  `(clojure.core/let [~@bindings
                      x# ~x]
     (-> x# ~@body)))

(defmacro cond
  [x & clauses]
  (list 'clojure.core/let
        ['x__ x]
        (cons 'clojure.core/cond
              (mapcat (fn [[pred & body]]
                        [pred (cons '-> (cons 'x__ body))])
                      (partition 2 clauses)))))

(defmacro as
  [x binding & body]
  (if (seq? binding)
    `(clojure.core/let [x# ~x
                        ~(last binding) (-> x# ~(drop-last binding))]
       (-> x# ~@body))
    `(clojure.core/let [x# ~x
                        ~binding x#]
       (-> x# ~@body))))

(defmacro aside
  [x binding & body]
  `(doto ~x (->/as ~binding (do ~@body))))

(defmacro in
  [x path & body]
  `(if (empty? ~path)
     (-> ~x ~@body)
     (update-in ~x ~path (fn [x#] (-> x# ~@body)))))
