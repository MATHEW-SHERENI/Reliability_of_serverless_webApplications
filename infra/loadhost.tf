# ---------- EC2 load host (M5 runs here) ----------
# A fixed-size, non-burstable 2-vCPU instance in the same region as the API.
# It has no SSH key and no inbound port: commands arrive through AWS Systems
# Manager (SSM), and run files go to the results bucket (stack infra/data).

# m7i-flex.large: 2 vCPU, 8 GiB, and on the account's free-plan list (m7i.large
# is refused there). Flex instances guarantee 40% CPU and run at full speed 95%
# of the time; a T3 instead slows down once its CPU credits run out. Each run's
# CPU is recorded, and a run above 70% is invalid.
variable "loadhost_type" {
  description = "2-vCPU load host; must be on the account's eligible instance list."
  type        = string
  default     = "m7i-flex.large"
}

locals {
  k6_version = "2.3.0"
  # SHA-256 of k6-v2.3.0-linux-amd64.tar.gz, from the release's checksums file.
  k6_sha256      = "39c3117b6af817592dcd0ce4242105c0a7af10948c2a425306f0be8f7a8a8ab1"
  results_bucket = "${var.project}-results-${data.aws_caller_identity.current.account_id}"
}

data "aws_caller_identity" "current" {}

# Latest Amazon Linux 2023 image at deploy time; its ID is an output and is
# recorded with every run.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_security_group" "loadhost" {
  name        = "${var.project}-loadhost"
  description = "k6 load host: no inbound traffic, outbound only"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "loadhost" {
  name               = "${var.project}-loadhost-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Lets the SSM agent on the instance receive commands.
resource "aws_iam_role_policy_attachment" "loadhost_ssm" {
  role       = aws_iam_role.loadhost.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Read the workload script and write run files, only under runs/ in the bucket.
data "aws_iam_policy_document" "loadhost" {
  statement {
    sid       = "RunFiles"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["arn:aws:s3:::${local.results_bucket}/runs/*"]
  }
}

resource "aws_iam_role_policy" "loadhost" {
  name   = "${var.project}-loadhost-runs"
  role   = aws_iam_role.loadhost.id
  policy = data.aws_iam_policy_document.loadhost.json
}

resource "aws_iam_instance_profile" "loadhost" {
  name = "${var.project}-loadhost-profile"
  role = aws_iam_role.loadhost.name
}

resource "aws_instance" "loadhost" {
  ami                         = data.aws_ssm_parameter.al2023.insecure_value
  instance_type               = var.loadhost_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.loadhost.id]
  iam_instance_profile        = aws_iam_instance_profile.loadhost.name
  associate_public_ip_address = true
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/loadhost_init.sh", {
    k6_version = local.k6_version
    k6_sha256  = local.k6_sha256
  })

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
  }

  tags = {
    Name = "${var.project}-loadhost"
  }

  depends_on = [aws_iam_role_policy_attachment.loadhost_ssm]
}
