// ── KubeAstra — Jenkins Pipeline ─────────────────────────────────────────────
//
// Builds and pushes two Docker images to GitHub Container Registry (ghcr.io):
//   ghcr.io/kubeastra/kubeastra-backend:<tag>
//   ghcr.io/kubeastra/kubeastra-frontend:<tag>
//
// Required Jenkins credentials (Manage Jenkins → Credentials):
//   ghcr-token  — Secret Text (GH PAT with write:packages scope)
//
// Optional parameters:
//   REGISTRY   — Docker registry host (default: ghcr.io/kubeastra)
//   IMAGE_TAG  — Override the auto-generated tag (git SHA + branch)
//
// GitHub Actions (`.github/workflows/ci.yml` + `evals.yml`) are the primary
// CI path for this repo. This Jenkinsfile is provided for self-hosted /
// air-gapped deployments where GHA isn't available.

pipeline {
    agent {
        label 'linux'
    }

    environment {
        REGISTRY       = 'ghcr.io/kubeastra'
        BACKEND_IMAGE  = "${REGISTRY}/kubeastra-backend"
        FRONTEND_IMAGE = "${REGISTRY}/kubeastra-frontend"
    }

    parameters {
        string(name: 'CUSTOM_TAG', defaultValue: '', description: 'Override image tag (leave blank for auto: git-SHA-branch)')
    }

    stages {

        // ── 1. Checkout ──────────────────────────────────────────────────────
        stage('Checkout') {
            steps {
                checkout scm
                script {
                    def shortSha = sh(script: 'git rev-parse --short HEAD', returnStdout: true).trim()
                    def rawBranch = (env.BRANCH_NAME ?: env.GIT_BRANCH ?: 'unknown')
                                        .replaceAll('^origin/', '')
                                        .replaceAll('[^a-zA-Z0-9._-]', '-')
                    env.IMAGE_TAG = params.CUSTOM_TAG ?: "${rawBranch}-${shortSha}"
                    echo "Building images with tag: ${env.IMAGE_TAG}"
                }
            }
        }

        // ── 2. Build & push backend ──────────────────────────────────────────
        // Workspace root IS the repo root — "." gives Docker access to both mcp/ and ui/backend/
        stage('Build & Push Backend') {
            steps {
                container('dotnet') {
                    withCredentials([string(
                        credentialsId: 'ghcr-token',
                        variable: 'GHCR_TOKEN'
                    )]) {
                        sh 'echo "$GHCR_TOKEN" | docker login ghcr.io -u kubeastra --password-stdin'
                        sh """
                            docker build \\
                              -f ui/backend/Dockerfile \\
                              -t ${BACKEND_IMAGE}:${IMAGE_TAG} \\
                              .
                            docker push ${BACKEND_IMAGE}:${IMAGE_TAG}
                        """
                    }
                }
            }
        }

        // ── 3. Build & push frontend ─────────────────────────────────────────
        stage('Build & Push Frontend') {
            steps {
                container('dotnet') {
                    withCredentials([string(
                        credentialsId: 'ghcr-token',
                        variable: 'GHCR_TOKEN'
                    )]) {
                        dir('ui/frontend') {
                            sh 'echo "$GHCR_TOKEN" | docker login ghcr.io -u kubeastra --password-stdin'
                            sh """
                                docker build \\
                                  -t ${FRONTEND_IMAGE}:${IMAGE_TAG} \\
                                  .
                                docker push ${FRONTEND_IMAGE}:${IMAGE_TAG}
                            """
                        }
                    }
                }
            }
        }

        // ── 4. Summary ───────────────────────────────────────────────────────
        stage('Summary') {
            steps {
                echo """
╔══════════════════════════════════════════════════════════════╗
║  Images pushed to ghcr.io                                    ║
╠══════════════════════════════════════════════════════════════╣
║  ${BACKEND_IMAGE}:${IMAGE_TAG}
║  ${FRONTEND_IMAGE}:${IMAGE_TAG}
╚══════════════════════════════════════════════════════════════╝

To deploy with Helm:
  helm upgrade --install kubeastra helm/kubeastra \\
    --namespace kubeastra --create-namespace \\
    --set backend.image.repository=${BACKEND_IMAGE} \\
    --set backend.image.tag=${IMAGE_TAG} \\
    --set frontend.image.repository=${FRONTEND_IMAGE} \\
    --set frontend.image.tag=${IMAGE_TAG} \\
    -f my-values.yaml
"""
            }
        }
    }

    post {
        success {
            echo "✅ Build and push succeeded — tag: ${IMAGE_TAG}"
        }
        failure {
            echo "❌ Pipeline failed — check logs above"
        }
    }
}
