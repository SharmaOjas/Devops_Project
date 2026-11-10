// CD pipeline: build a new image version and roll it out to the local Docker Desktop Kubernetes cluster.
// GitHub Actions (CI) tests every push on a throwaway cluster; Jenkins (CD) deploys to our real one.
// Written for Jenkins running as a Windows service, hence bat/powershell steps.
pipeline {
    agent any

    environment {
        IMAGE      = "dark-ship-watch:${env.BUILD_NUMBER}"   // every build is a new version -> real rolling update
        KUBECONFIG = 'C:\\Users\\ojass\\.kube\\config'          // the service account has no kubeconfig of its own
    }

    options {
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
    }

    stages {
        stage('Build image') {
            steps {
                bat 'docker build -t %IMAGE% .'
            }
        }

        stage('Deploy database') {
            steps {
                bat 'kubectl apply -f k8s/db-statefulset.yaml -f k8s/service.yaml'
                bat 'kubectl rollout status statefulset/postgis --timeout=180s'
            }
        }

        stage('Migrate database + deploy app') {
            steps {
                // A Job's pod template is immutable, so the setup Job is recreated on every deploy.
                // setup_db.py is idempotent, so this doubles as the schema migration step.
                bat 'kubectl delete job dsw-db-init --ignore-not-found'
                powershell '(Get-Content k8s/app-deployment.yaml -Raw) -replace "dark-ship-watch:1.0", $env:IMAGE | kubectl apply -f -'
            }
        }

        stage('Wait for rollout') {
            steps {
                bat 'kubectl wait --for=condition=complete job/dsw-db-init --timeout=300s'
                bat 'kubectl rollout status deployment/dark-ship-app --timeout=180s'
                bat 'kubectl rollout status deployment/dark-ship-collector --timeout=180s'
            }
        }

        stage('Smoke test') {
            steps {
                powershell '''
                    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 http://localhost:30000/api/health
                    if ($r.StatusCode -ne 200) { throw "health check returned $($r.StatusCode)" }
                    $r.Content
                '''
                bat 'kubectl get pods,svc,pvc'
            }
        }
    }

    post {
        success {
            echo "Deployed ${env.IMAGE}: http://localhost:30000"
        }
        failure {
            echo 'Deploy failed: rolling back to the previous version'
            bat 'kubectl rollout undo deployment/dark-ship-app & kubectl rollout undo deployment/dark-ship-collector & exit /b 0'
        }
    }
}
