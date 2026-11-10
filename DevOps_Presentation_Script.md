# Dark Ship Watch: 10-minute DevOps presentation script

Group 17, Model Deployment using Kubernetes. Each person speaks for about 3 minutes 20 seconds, at a calm pace of about 130 words per minute. The same lines are in each slide's speaker notes.

## Ojas: slides 1 to 4 (about 428 words, ~3.3 min)

**Slide 1: Dark ship watch** (~0:30)

Good morning sir. We are Group 17: I am Ojas Sharma, with Shamika Daga and Tanusha Daga. Our topic is Model Deployment using Kubernetes. We took Dark Ship Watch, a live ship-tracking system with a going-dark detection model, and turned it into a proper DevOps deployment on Kubernetes. I will cover the project and the problem, Shamika will explain the architecture and the pipeline, and Tanusha will show the model, the results and the demo.

**Slide 2: Introduction to our project** (~0:50)

First, what the application is. Every large ship carries a radio called AIS that keeps sending its position, speed and heading. The Finnish government shares this data for free. Our app has three parts. A collector pulls new positions every minute and weather every ten minutes. A PostgreSQL database with the PostGIS extension stores and checks every record. And a web app shows a live map of the Gulf of Finland, together with our model, which finds ships that switch their radio off. We first built this for our database course. There it ran on one laptop, and we started it by hand with a batch file.

**Slide 3: Problem statement** (~0:50)

That is exactly the problem DevOps tries to solve. On one laptop, everything is a single point of failure. If the laptop sleeps, the collector stops. If the database crashes, the website shows errors until someone restarts it by hand. Moving it to another machine meant installing Python, PostgreSQL and PostGIS step by step, and hoping the versions matched. And there was no safe way to update it: stopping the app to change it meant downtime. So our goal was clear: make the deployment reproducible, self-healing, and updatable with zero downtime. As the slide says, works on my laptop is not deployed.

**Slide 4: Our DevOps toolchain** (~1:10)

Here is our toolchain. On the build side we use Docker. We wrote one Dockerfile based on the python 3.10 slim image. It installs our requirements, copies the code and starts the web app on port 5000. For the database we did not build anything ourselves: we use the official PostGIS image, version 15 with PostGIS 3.3, which already has the spatial extension. A dockerignore file keeps our local database files, backups and logs out of the image, so it stays small. On the deploy side we use Kubernetes on Docker Desktop. The whole system is written as YAML manifests in one k8s folder, and we apply them with kubectl. Passwords are stored in a Kubernetes Secret, and settings such as the database host in a ConfigMap. So the same image works on any cluster without changing code. Now Shamika will explain the architecture.

## Shamika: slides 5 to 7 (about 436 words, ~3.4 min)

**Slide 5: Architecture on Kubernetes** (~1:10)

Thank you Ojas. This is how the system looks inside the cluster. It has three tiers. The first is the database tier. PostGIS runs as a StatefulSet, not a normal Deployment, because a database needs a stable identity and its own storage. Its pod is always called postgis-0, and it has a two gigabyte persistent volume. The second tier is the model and web tier. This is a Deployment with two replicas. Each pod serves the map, the REST API and the going-dark model. Two replicas means that if one pod dies, the other keeps serving. The third part is the collector. It is also a Deployment, but with exactly one replica, because two collectors running together would write the same ship positions twice. For the same reason its update strategy is Recreate, so the old one stops before the new one starts.

**Slide 6: Deployment pipeline** (~1:15)

Next is our deployment pipeline, from code to a running app, in five steps. Step one, code: the application, the database schema and the k8s folder live in one project. Step two, build: one docker build command creates the image dark-ship-watch version 1.0. Step three, apply: kubectl apply with the k8s folder creates every object in one go. This is declarative: we describe the state we want, and Kubernetes makes it true. Step four, initialise: a one-time Kubernetes Job runs our setup script. It creates all 21 tables, the triggers and the database users, and loads the land map. We made it a Job, not part of the app, because two replicas setting up the schema at the same time would clash. Step five, serve: pods get traffic only after their health checks pass. One important point: one image plays three roles. On our cluster, the whole stack was running in under three minutes.

**Slide 7: Kubernetes objects we used** (~1:10)

These are the Kubernetes objects behind it. First, the StatefulSet and the PersistentVolumeClaim. The claim asks the cluster for two gigabytes of storage, and it stays even if the pod is deleted, so the ship history is safe. Second, the Deployment with probes. The readiness probe calls our /api/health endpoint, which queries the database, so a pod gets traffic only when it can really answer. The liveness probe calls the home page, which does not need the database. That way a database restart does not make Kubernetes kill healthy app pods. We also set maxUnavailable to zero, which is the key to zero-downtime updates. Third, the Services. A ClusterIP service gives the database a fixed internal address, postgis on port 5432. A LoadBalancer service opens the map on port 30000. Now Tanusha will show the model and our results.

## Tanusha: slides 8 to 12 (about 447 words, ~3.4 min)

**Slide 8: The model we deploy** (~0:55)

Thank you Shamika. Now, the model we deploy. A ship is called dark when it goes silent for more than 20 minutes without a normal reason. Our model checks four normal reasons: was it in a port, was it parked, did it leave our area, or did nearby ships go silent too, which means a radio outage. If none of these fit, the ship is flagged as going dark. Then the model draws the possible area: the sea the ship could have reached, using its top speed, the wind and the waves. On the screen is a real case, the cargo ship TONGAN, silent for 3 hours and 41 minutes. In the cluster, this model runs every five minutes inside the collector pod.

**Slide 9: Live demo: localhost:30000** (~0:40)

This is the live map, served from the cluster at localhost port 30000. Each request goes through the LoadBalancer to one of the two app pods. Every arrow is a real ship: blue is moving, grey is parked, white is in port and red is going dark. Minutes after deploying, the cluster was already tracking more than 440 ships with live data. In the demo we will also run kubectl get pods, svc and pvc, to show every object running.

**Slide 10: What we proved on the cluster** (~1:10)

Here is what we proved on the cluster, with real numbers. First, zero downtime. We ran kubectl rollout restart, which replaces every app pod, and at the same time we kept calling the health endpoint. 56 checks were sent and all 56 passed, because Kubernetes starts a new pod and waits for it to be ready before it stops an old one. Second, persistence. We deleted the database pod on purpose. Kubernetes created it again, attached the same volume, and all 1,141 rows were still there. Third, self-healing. We did not restart anything by hand: Kubernetes noticed the missing pod and recreated it, and probes keep traffic away from pods that are not ready yet. Fourth, scaling and rollback. One command, kubectl scale, adds more pods, and kubectl rollout undo goes back to the previous version.

**Slide 11: Conclusion what we learned** (~0:45)

To conclude. We took a system that ran on one laptop and made it a reproducible deployment. Docker gives us one image that runs the same everywhere, and YAML manifests describe the whole system as code. Kubernetes gives us self-healing, zero-downtime updates, and storage that survives restarts. We also know our limits: there is a single database pod, the passwords are demo values, and we build images by hand on a local cluster. Our next steps are a CI/CD pipeline with GitHub Actions or Jenkins, autoscaling with HPA, and monitoring with Prometheus and Grafana.

**Slide 12: Thank you!** (~0:10)

Thank you. We are happy to show the live demo and take your questions.

## If sir asks for a live demo (outside the 10 minutes)

Run these from the project folder with Docker Desktop's Kubernetes running:

```bash
kubectl get pods,svc,pvc
kubectl rollout restart deployment dark-ship-app
kubectl rollout status deployment dark-ship-app
kubectl delete pod postgis-0
kubectl get pods -w
```

Then open http://localhost:30000 to show the map still working.
